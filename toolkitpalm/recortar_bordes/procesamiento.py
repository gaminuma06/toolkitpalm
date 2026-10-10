# -*- coding: utf-8 -*-
"""
Recorte del marco de relleno de una ortofoto.

Muchas ortofotos traen un relleno de color plano (gris, blanco, negro o cualquier
otro) alrededor de la zona realmente fotografiada, sin marcarlo como "sin datos".
Este módulo lo detecta y genera una copia recortada, con el relleno transparente.

Es marco todo lo que, a la vez:
  - tiene uno de los colores de relleno detectados en el borde,
  - es plano (sin textura: un camino o un techo fotografiado siempre tiene),
  - y está unido al borde de la imagen.
Así un camino gris dentro de la foto no se borra aunque sea del color del marco.

Solo usa GDAL/OGR y numpy, que vienen con cualquier QGIS.
"""
import logging
import math
import os
import shutil
import tempfile

import numpy
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

logger = logging.getLogger(__name__)

LADO_ANALISIS = 3000          # píxeles del lado mayor de la versión reducida
BORDE_IGNORADO = 2            # filos de este ancho (compresión, líneas) cuentan siempre como marco
PROFUNDIDADES = (2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64)  # anillos donde se buscan los colores
FRACCION_COLOR_MARCO = 0.05   # un color es "de marco" si ocupa al menos esto de esos anillos
FRACCION_PARTE_MINIMA = 0.02  # partes de foto más pequeñas que esto (vs. la mayor) se descartan
FRACCION_HUECO_MINIMO = 0.0005  # huecos planos interiores mayores que esto (vs. la imagen) son relleno
# Refinado del borde a resolución completa (en la franja alrededor del contorno aproximado)
BANDA_REFINADO = 8.0          # semiancho de la franja, en píxeles de análisis
RADIO_SOLIDO = 3              # ventana (7x7) para decidir que una zona es relleno sólido
FRACCION_SOLIDO = 0.9         # fracción de la ventana que debe tener color de relleno
DILATACION_FINA = 2           # píxeles reales que se quitan de más en el límite (filo mezclado)
BORDE_FINO = 4                # píxeles reales del canto de la imagen que siempre son relleno
BLOQUE = 1024                 # tamaño de bloque al procesar la franja
BLOQUES_POR_APERTURA = 40     # cada cuántos bloques se reabre el archivo (libera la caché de ECW)


class SinMarcoError(Exception):
    pass


class Cancelado(Exception):
    pass


def _leer_reducida(ds, bandas):
    escala = max(1.0, max(ds.RasterXSize, ds.RasterYSize) / float(LADO_ANALISIS))
    ancho = max(1, int(round(ds.RasterXSize / escala)))
    alto = max(1, int(round(ds.RasterYSize / escala)))
    capas = [ds.GetRasterBand(b).ReadAsArray(buf_xsize=ancho, buf_ysize=alto,
                                             resample_alg=gdal.GRIORA_NearestNeighbour)
             for b in bandas]
    # Píxeles que el propio archivo ya marca como vacíos (sin datos, alfa en 0…).
    mascara = ds.GetRasterBand(bandas[0]).GetMaskBand().ReadAsArray(
        buf_xsize=ancho, buf_ysize=alto, resample_alg=gdal.GRIORA_NearestNeighbour)
    return numpy.stack(capas, axis=-1).astype(numpy.int16), mascara == 0, ancho, alto


def _plano(img, umbral):
    """True donde el vecindario 3x3 es de un solo color en todas las bandas."""
    alto, ancho = img.shape[:2]
    p = numpy.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
    maximo = img.copy()
    minimo = img.copy()
    for dy in range(3):
        for dx in range(3):
            v = p[dy:dy + alto, dx:dx + ancho]
            numpy.maximum(maximo, v, out=maximo)
            numpy.minimum(minimo, v, out=minimo)
    return numpy.all(maximo - minimo <= umbral, axis=-1)


def _anillos(arr, alto, ancho):
    partes = []
    for d in PROFUNDIDADES:
        if 2 * d + 2 >= min(alto, ancho):
            break
        partes += [arr[d, d:ancho - d], arr[alto - 1 - d, d:ancho - d],
                   arr[d:alto - d, d], arr[d:alto - d, ancho - 1 - d]]
    return numpy.concatenate(partes)


def _colores_del_marco(img, plano, vacio, tolerancia):
    """Colores planos dominantes en una franja cerca del borde (sin el filo exterior)."""
    alto, ancho = img.shape[:2]
    pixeles = _anillos(img, alto, ancho)
    restantes = pixeles[_anillos(plano & ~vacio, alto, ancho)]
    colores = []
    while len(restantes):
        # El color más repetido (no la mediana: con un marco de dos colores, la
        # mediana cae en un tono intermedio que no existe en la imagen).
        valores, cuentas = numpy.unique(restantes, axis=0, return_counts=True)
        moda = valores[numpy.argmax(cuentas)]
        cerca = numpy.all(numpy.abs(restantes - moda) <= tolerancia, axis=1)
        if cerca.sum() < FRACCION_COLOR_MARCO * len(pixeles):
            break
        colores.append(numpy.median(restantes[cerca], axis=0))
        restantes = restantes[~cerca]
    return colores


def _parece_marco(img, plano, vacio, colores, tolerancia):
    parecido = numpy.zeros(img.shape[:2], dtype=bool)
    for c in colores:
        parecido |= numpy.all(numpy.abs(img - c) <= tolerancia, axis=-1)
    marco = (parecido & plano) | vacio
    b = BORDE_IGNORADO
    marco[:b, :] = marco[-b:, :] = marco[:, :b] = marco[:, -b:] = True
    return marco


def _huella_de_la_foto(marco, ds, ancho, alto):
    """Polígono de la foto real: la imagen menos el marco unido al borde."""
    gt = ds.GetGeoTransform()
    gt_red = (gt[0], gt[1] * ds.RasterXSize / ancho, gt[2],
              gt[3], gt[4], gt[5] * ds.RasterYSize / alto)

    mem = gdal.GetDriverByName("MEM").Create("", ancho, alto, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(gt_red)
    mem.GetRasterBand(1).WriteArray(marco.astype(numpy.uint8))

    vds = ogr.GetDriverByName("Memory").CreateDataSource("")
    capa = vds.CreateLayer("m", srs=None)
    capa.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    banda = mem.GetRasterBand(1)
    gdal.Polygonize(banda, banda, capa, 0, ["8CONNECTED=8"])

    x0, y0 = gt_red[0], gt_red[3]
    x1, y1 = x0 + gt_red[1] * ancho, y0 + gt_red[5] * alto
    caja = ogr.CreateGeometryFromWkt(
        f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))")
    contorno_caja = caja.Boundary()

    marco_total = ogr.Geometry(ogr.wkbMultiPolygon)
    hueco_minimo = FRACCION_HUECO_MINIMO * caja.GetArea()
    for f in capa:
        g = f.GetGeometryRef()
        # Relleno unido al borde, o un hueco interior grande (zona sin foto dentro
        # del mosaico). Las manchas planas pequeñas, como un techo blanco, se quedan.
        if g.Intersects(contorno_caja) or g.GetArea() >= hueco_minimo:
            marco_total.AddGeometry(g.Clone())
    huella = caja.Difference(marco_total.UnionCascaded())

    partes = [huella.GetGeometryRef(i).Clone() for i in range(huella.GetGeometryCount())] \
        if huella.GetGeometryType() in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection) \
        else [huella]
    partes = [p for p in partes if p.GetGeometryType() == ogr.wkbPolygon and not p.IsEmpty()]
    if not partes:
        raise SinMarcoError("Todo parece relleno: no se encontró foto dentro del ráster.")
    mayor = max(p.GetArea() for p in partes)
    union = ogr.Geometry(ogr.wkbMultiPolygon)
    for p in partes:
        if p.GetArea() >= FRACCION_PARTE_MINIMA * mayor:
            union.AddGeometry(p)

    # Contorno aproximado: el borde exacto se decide después a resolución completa.
    tam = abs(gt_red[1])
    return union.UnionCascaded().Simplify(0.5 * tam), caja.GetArea()


def detectar(entrada, tolerancia=12):
    """Solo detecta: devuelve (huella, info) sin escribir nada. Es rápido."""
    ds = gdal.Open(entrada)
    if ds.GetGeoTransform() == (0, 1, 0, 0, 0, 1):
        raise SinMarcoError("El ráster no está georreferenciado.")
    bandas = [b for b in range(1, ds.RasterCount + 1)
              if ds.GetRasterBand(b).GetColorInterpretation() != gdal.GCI_AlphaBand]

    img, vacio, ancho, alto = _leer_reducida(ds, bandas)
    plano = _plano(img, max(2, tolerancia // 4))
    colores = _colores_del_marco(img, plano, vacio, tolerancia)
    if not colores and not vacio.any():
        raise SinMarcoError(
            "No se detectó ningún marco: cerca del borde no hay un color plano. "
            "Puede que la foto ya esté recortada.")
    marco = _parece_marco(img, plano, vacio, colores, tolerancia)
    huella, area_total = _huella_de_la_foto(marco, ds, ancho, alto)
    return huella, {
        "colores": [tuple(int(round(x)) for x in c) for c in colores],
        "recorte_pct": 100.0 * (1 - huella.GetArea() / area_total),
        "proyeccion": ds.GetProjection(),
        "bandas": bandas,
        "tiene_alfa": len(bandas) < ds.RasterCount,
        "tipo": ds.GetRasterBand(bandas[0]).DataType,
        "tam": (ds.RasterXSize, ds.RasterYSize),
        "gt": ds.GetGeoTransform(),
        "pixel": (abs(ds.GetGeoTransform()[1]), abs(ds.GetGeoTransform()[5])),
        "escala": ds.RasterXSize / float(ancho),
        "colores_reales": [list(map(float, c)) for c in colores],
        "tiene_vacio": bool(vacio.any()),
        "tolerancia": tolerancia,
    }


def _guardar_huella(huella, proyeccion, ruta):
    srs = None
    if proyeccion:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(proyeccion)
    vds = ogr.GetDriverByName("GPKG").CreateDataSource(ruta)
    capa = vds.CreateLayer("huella", srs=srs, geom_type=ogr.wkbMultiPolygon)
    f = ogr.Feature(capa.GetLayerDefn())
    f.SetGeometry(ogr.ForceToMultiPolygon(huella))
    capa.CreateFeature(f)


def _media_caja(m, r):
    """Promedio de `m` en una ventana (2r+1)x(2r+1), con imagen integral."""
    k = 2 * r + 1
    c = numpy.pad(m.astype(numpy.int32), r, mode="edge").cumsum(0).cumsum(1)
    c = numpy.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / float(k * k)


def _marco_fino(img, vacio, colores, tolerancia):
    """Relleno a resolución completa: zonas sólidas del color de relleno.

    Se exige que la ventana 7x7 sea casi toda del color de relleno: así un píxel
    oscuro suelto de la foto no se confunde con el marco, y el ruido de la
    compresión dentro del marco no deja puntos sin quitar.
    """
    cerca = vacio.copy()
    for c in colores:
        cerca |= numpy.all(numpy.abs(img - c) <= tolerancia, axis=-1)
    solido = _media_caja(cerca, RADIO_SOLIDO) >= FRACCION_SOLIDO
    marco = cerca & (_media_caja(solido, RADIO_SOLIDO) > 0)
    # El límite entre foto y relleno queda mezclado por la compresión: se quitan
    # un par de píxeles más para que no quede un filo del color del marco.
    return _media_caja(marco, DILATACION_FINA) > 0


def _refinar_borde(mascara, x0, y0, huella, info, entrada, avisar, cancelado):
    """Reescribe la máscara en la franja del contorno, decidiendo píxel por píxel."""
    gt = info["gt"]
    tam_analisis = abs(gt[1]) * info["escala"]
    franja = huella.Boundary().Buffer(BANDA_REFINADO * tam_analisis)
    mem_vec = ogr.GetDriverByName("Memory").CreateDataSource("")
    srs = None
    if info["proyeccion"]:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(info["proyeccion"])
    capa_franja = mem_vec.CreateLayer("franja", srs=srs)
    f = ogr.Feature(capa_franja.GetLayerDefn())
    f.SetGeometry(franja)
    capa_franja.CreateFeature(f)

    src = gdal.Open(entrada)
    ancho_src, alto_src = src.RasterXSize, src.RasterYSize
    bandas = [src.GetRasterBand(b) for b in info["bandas"]]
    banda_mascara_src = bandas[0].GetMaskBand() if info["tiene_vacio"] else None
    colores = [numpy.array(c) for c in info["colores_reales"]]
    banda_salida = mascara.GetRasterBand(1)
    ancho, alto = mascara.RasterXSize, mascara.RasterYSize
    gx, gy = gt[0] + x0 * gt[1], gt[3] + y0 * gt[5]
    pad = RADIO_SOLIDO * 2 + DILATACION_FINA + 1

    bloques = []
    for ty in range(0, alto, BLOQUE):
        for tx in range(0, ancho, BLOQUE):
            w, h = min(BLOQUE, ancho - tx), min(BLOQUE, alto - ty)
            ax, ay = gx + tx * gt[1], gy + ty * gt[5]
            bx, by = ax + w * gt[1], ay + h * gt[5]
            caja = ogr.CreateGeometryFromWkt(
                f"POLYGON(({ax} {ay},{bx} {ay},{bx} {by},{ax} {by},{ax} {ay}))")
            if caja.Intersects(franja):
                bloques.append((tx, ty, w, h))

    for i, (tx, ty, w, h) in enumerate(bloques):
        if cancelado and cancelado():
            raise Cancelado()
        avisar(i / max(1, len(bloques)))
        if i and i % BLOQUES_POR_APERTURA == 0:
            # Algunos formatos (ECW) acumulan caché sin límite mientras el archivo
            # está abierto; reabrirlo cada tanto la libera.
            bandas = banda_mascara_src = None
            src = None
            src = gdal.Open(entrada)
            bandas = [src.GetRasterBand(b) for b in info["bandas"]]
            banda_mascara_src = bandas[0].GetMaskBand() if info["tiene_vacio"] else None

        en_franja = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
        en_franja.SetGeoTransform((gx + tx * gt[1], gt[1], 0, gy + ty * gt[5], 0, gt[5]))
        if info["proyeccion"]:
            en_franja.SetProjection(info["proyeccion"])
        gdal.RasterizeLayer(en_franja, [1], capa_franja, burn_values=[1])
        en_franja = en_franja.GetRasterBand(1).ReadAsArray() > 0

        # Lectura con margen (recortado a los límites del archivo y rellenado).
        sx, sy = x0 + tx - pad, y0 + ty - pad
        lx, ly = max(0, sx), max(0, sy)
        rx, ry = min(ancho_src, sx + w + 2 * pad), min(alto_src, sy + h + 2 * pad)
        img = numpy.stack([b.ReadAsArray(lx, ly, rx - lx, ry - ly) for b in bandas],
                          axis=-1).astype(numpy.int16)
        vacio = (banda_mascara_src.ReadAsArray(lx, ly, rx - lx, ry - ly) == 0
                 if banda_mascara_src else numpy.zeros(img.shape[:2], dtype=bool))
        relleno = ((ly - sy, sy + h + 2 * pad - ry), (lx - sx, sx + w + 2 * pad - rx))
        img = numpy.pad(img, relleno + ((0, 0),), mode="edge")
        vacio = numpy.pad(vacio, relleno, mode="edge")

        marco = _marco_fino(img, vacio, colores, info["tolerancia"])[pad:pad + h, pad:pad + w]

        # El canto de la imagen (líneas de la compresión) siempre es relleno.
        cols = numpy.arange(x0 + tx, x0 + tx + w)
        filas = numpy.arange(y0 + ty, y0 + ty + h)
        marco[:, (cols < BORDE_FINO) | (cols >= ancho_src - BORDE_FINO)] = True
        marco[(filas < BORDE_FINO) | (filas >= alto_src - BORDE_FINO), :] = True

        actual = banda_salida.ReadAsArray(tx, ty, w, h)
        nuevo = numpy.where(en_franja, numpy.where(marco, 0, 255), actual).astype(numpy.uint8)
        banda_salida.WriteArray(nuevo, tx, ty)


def _vrt_con_mascara(entrada, huella, info, ruta_huella, carpeta, vrt,
                     avisar=lambda f: None, cancelado=None):
    """VRT con las bandas de color recortadas al rectángulo de la foto y una máscara
    dibujada desde la huella.

    Copiar la imagen y dibujar la máscara aparte es mucho más rápido que recortar
    con gdal.Warp, que evalúa el contorno píxel por píxel.

    Devuelve (bandas, mascara) para gdal.Translate.
    """
    gt = info["gt"]
    ancho_total, alto_total = info["tam"]
    n = len(info["bandas"])
    if gt[2] or gt[4]:
        # Ráster rotado (muy raro): se usa el camino general, más lento. La
        # transparencia de Warp queda como última banda y se pasa como máscara.
        gdal.Warp(vrt, entrada, options=gdal.WarpOptions(
            format="VRT", cutlineDSName=ruta_huella, cropToCutline=True,
            srcBands=info["bandas"], dstAlpha=True,
            multithread=True, warpOptions=["NUM_THREADS=ALL_CPUS"]))
        return list(range(1, n + 1)), n + 1

    minx, maxx, miny, maxy = huella.GetEnvelope()
    x0 = max(0, int(math.floor((minx - gt[0]) / gt[1])))
    x1 = min(ancho_total, int(math.ceil((maxx - gt[0]) / gt[1])))
    y0 = max(0, int(math.floor((maxy - gt[3]) / gt[5])))
    y1 = min(alto_total, int(math.ceil((miny - gt[3]) / gt[5])))
    ancho, alto = x1 - x0, y1 - y0
    gt_ventana = (gt[0] + x0 * gt[1], gt[1], 0.0, gt[3] + y0 * gt[5], 0.0, gt[5])

    ruta_mascara = os.path.join(carpeta, "mascara.tif")
    m = gdal.GetDriverByName("GTiff").Create(
        ruta_mascara, ancho, alto, 1, gdal.GDT_Byte,
        ["TILED=YES", "COMPRESS=DEFLATE", "SPARSE_OK=TRUE", "BIGTIFF=IF_SAFER"])
    m.SetGeoTransform(gt_ventana)
    if info["proyeccion"]:
        m.SetProjection(info["proyeccion"])
    vds = ogr.Open(ruta_huella)
    gdal.RasterizeLayer(m, [1], vds.GetLayer(0), burn_values=[255])
    vds = None
    _refinar_borde(m, x0, y0, huella, info, entrada, avisar, cancelado)
    m = None

    gdal.Translate(vrt, entrada, options=gdal.TranslateOptions(
        format="VRT", srcWin=[x0, y0, ancho, alto], bandList=info["bandas"], maskBand="none"))
    mascara_xml = (
        "<MaskBand><VRTRasterBand dataType=\"Byte\"><SimpleSource>"
        f"<SourceFilename relativeToVRT=\"1\">{os.path.basename(ruta_mascara)}</SourceFilename>"
        "<SourceBand>1</SourceBand></SimpleSource></VRTRasterBand></MaskBand>")
    texto = open(vrt, encoding="utf-8").read()
    texto = texto.replace("</VRTDataset>", mascara_xml + "</VRTDataset>", 1)
    open(vrt, "w", encoding="utf-8").write(texto)
    return list(range(1, n + 1)), "mask"


def recortar(entrada, salida, tolerancia=12, progreso=None, cancelado=None):
    """Genera `salida` (GeoTIFF) con el marco de `entrada` transparente y recortado.

    `progreso(fraccion, texto)` informa avances entre 0 y 1; `cancelado()` puede
    devolver True para detener. Devuelve la info de la detección.
    """
    def avisar(f, t):
        if progreso:
            progreso(f, t)

    def callback(inicio, fin, texto):
        def cb(fraccion, *_):
            avisar(inicio + (fin - inicio) * fraccion, texto)
            return 0 if (cancelado and cancelado()) else 1
        return cb

    avisar(0.02, "Detectando el marco…")
    huella, info = detectar(entrada, tolerancia)
    if cancelado and cancelado():
        raise Cancelado()

    carpeta = tempfile.mkdtemp(prefix="tkp_recorte_")
    try:
        ruta_huella = os.path.join(carpeta, "huella.gpkg")
        _guardar_huella(huella, info["proyeccion"], ruta_huella)

        avisar(0.04, "Ajustando el borde a resolución completa…")
        vrt = os.path.join(carpeta, "recorte.vrt")
        lista_bandas, banda_mascara = _vrt_con_mascara(
            entrada, huella, info, ruta_huella, carpeta, vrt,
            avisar=lambda f: avisar(0.04 + 0.21 * f, "Ajustando el borde a resolución completa…"),
            cancelado=cancelado)
        if cancelado and cancelado():
            raise Cancelado()

        n = len(info["bandas"])
        jpeg = n == 3 and info["tipo"] == gdal.GDT_Byte
        opciones = ["TILED=YES", "BIGTIFF=IF_SAFER", "NUM_THREADS=ALL_CPUS"]
        opciones += (["COMPRESS=JPEG", "PHOTOMETRIC=YCBCR", "JPEG_QUALITY=90"] if jpeg
                     else ["COMPRESS=DEFLATE", "PREDICTOR=2"])

        gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")
        gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
        try:
            # La transparencia va como máscara interna: permite comprimir en JPEG
            # (que no admite una cuarta banda) y QGIS la respeta igual.
            gdal.Translate(salida, vrt, options=gdal.TranslateOptions(
                format="GTiff", bandList=lista_bandas, maskBand=banda_mascara,
                creationOptions=opciones,
                callback=callback(0.25, 0.80, "Escribiendo la ortofoto recortada…")))

            # Pirámides: sin ellas QGIS no logra dibujar una ortofoto grande.
            gdal.SetConfigOption("COMPRESS_OVERVIEW", "JPEG" if jpeg else "DEFLATE")
            if jpeg:
                gdal.SetConfigOption("PHOTOMETRIC_OVERVIEW", "YCBCR")
                gdal.SetConfigOption("INTERLEAVE_OVERVIEW", "PIXEL")
                gdal.SetConfigOption("JPEG_QUALITY_OVERVIEW", "85")
            ds = gdal.Open(salida, gdal.GA_Update)
            niveles, lado = [], max(ds.RasterXSize, ds.RasterYSize)
            while lado // (2 ** (len(niveles) + 1)) >= 256:
                niveles.append(2 ** (len(niveles) + 1))
            if niveles:
                ds.BuildOverviews("AVERAGE", niveles,
                                  callback=callback(0.80, 1.0, "Generando pirámides para verla rápido…"))
            ds = None
        finally:
            for opcion in ("GDAL_TIFF_INTERNAL_MASK", "GDAL_NUM_THREADS", "COMPRESS_OVERVIEW",
                           "PHOTOMETRIC_OVERVIEW", "INTERLEAVE_OVERVIEW", "JPEG_QUALITY_OVERVIEW"):
                gdal.SetConfigOption(opcion, None)
    except (Cancelado, RuntimeError):
        if cancelado and cancelado():
            # No dejar un archivo a medio escribir.
            for ruta in (salida, salida + ".aux.xml", salida + ".msk"):
                try:
                    os.remove(ruta)
                except OSError:
                    logger.debug("No se pudo borrar %s", ruta, exc_info=True)
            raise Cancelado()
        raise
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)

    avisar(1.0, "Listo.")
    return info
