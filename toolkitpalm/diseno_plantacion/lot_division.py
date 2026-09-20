# -*- coding: utf-8 -*-
"""
Núcleo geométrico del Diseñador de Plantación — Fase 1: orientación del
cultivo y división del predio en lotes/bloques con corredores para vías y
drenajes.

Deliberadamente usa solo QgsGeometry/GDAL/numpy (sin shapely ni otras
dependencias nuevas), igual que el resto del plugin, para que corra dentro
del Python de QGIS sin instalar nada adicional.

Convenciones de esta primera versión (a validar con datos reales de una
finca antes de dar por buena la Fase 1):
- El predio y el DEM deben estar en un CRS proyectado en metros (no
  geográfico) — si no, hay que reproyectar antes de llamar a estas funciones.
- bearing_deg es el rumbo de las LÍNEAS DE SIEMBRA (no de las vías), medido
  en grados desde el norte, sentido horario. 0° = filas exactamente Norte-Sur.
- La malla de lotes es rectangular por columnas (perpendiculares al rumbo) y
  filas (a lo largo del rumbo); se recorta contra el polígono real del predio,
  así que los lotes de borde salen irregulares — tal como se pidió.
"""
import logging
import math

import numpy as np
from qgis.PyQt.QtCore import QVariant
from qgis.core import (QgsGeometry, QgsRectangle, QgsFeature, QgsField,
                        QgsVectorLayer, QgsCoordinateReferenceSystem,
                        QgsCoordinateTransform, QgsProject)

M2_PER_HA = 10000.0


def get_appropriate_utm_crs(lon, lat):
    """Determina el EPSG de la zona UTM apropiada para una coordenada geográfica."""
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return QgsCoordinateReferenceSystem(f"EPSG:{epsg}")


def resolve_working_crs(predio_crs, dem_crs, polygon_geom_in_predio_crs):
    """
    Decide en qué CRS proyectado (metros) trabajar: si el CRS del predio ya es
    proyectado, se usa ese. Si no lo es pero el del DEM sí, se usa el del DEM.
    Si ambos son geográficos (grados), se calcula la zona UTM apropiada según
    el centroide del predio — mismo criterio que ya usa el Segmentador.
    """
    if not predio_crs.isGeographic():
        return predio_crs
    if not dem_crs.isGeographic():
        return dem_crs

    centroid = polygon_geom_in_predio_crs.centroid().asPoint()
    if predio_crs.authid() != "EPSG:4326":
        transform = QgsCoordinateTransform(predio_crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        centroid = transform.transform(centroid)
    return get_appropriate_utm_crs(centroid.x(), centroid.y())


def reproject_geometry(geom, src_crs, dst_crs):
    """Reproyecta una QgsGeometry de src_crs a dst_crs (copia; no modifica geom)."""
    if src_crs.authid() == dst_crs.authid():
        return QgsGeometry(geom)
    transform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
    g = QgsGeometry(geom)
    g.transform(transform)
    return g


def reproject_dem_if_needed(dem_path, dem_crs, dst_crs, tmp_dir):
    """
    Si dem_crs difiere de dst_crs, reproyecta el DEM a un GeoTIFF temporal
    (dentro de tmp_dir) con GDAL y retorna la nueva ruta. Si ya coincide,
    retorna dem_path tal cual (no copia nada innecesariamente).
    """
    if dem_crs.authid() == dst_crs.authid():
        return dem_path
    import os
    from osgeo import gdal
    gdal.UseExceptions()
    out_path = os.path.join(tmp_dir, "dem_reproyectado.tif")
    gdal.Warp(out_path, dem_path, dstSRS=dst_crs.toWkt(), resampleAlg="bilinear")
    return out_path


def compute_slope_and_aspect(dem_path, polygon_geom, max_pixels_per_side=1500):
    """
    Calcula la pendiente media (%) y el rumbo dominante de la ladera (aspecto,
    grados desde el norte, sentido horario) dentro del rectángulo envolvente
    del polígono, usando el DEM. Se recorta por el bounding box, no por la
    forma exacta del polígono — suficiente para decidir orientación general,
    no para diseño de detalle punto a punto.

    La lectura se reduce (downsample) a como mucho max_pixels_per_side por
    lado: para decidir una orientación general no hace falta la resolución
    completa, y leer un DEM/ráster grande a full resolución puede pedir
    decenas de GB de RAM (pasó exactamente eso con una ortofoto de
    ~125k x 129k px pasada por error como si fuera el DEM).
    """
    from osgeo import gdal
    gdal.UseExceptions()

    ds = gdal.Open(dem_path)
    if ds is None:
        raise Exception(f"No se pudo abrir el DEM: {dem_path}")

    if ds.RasterCount != 1:
        ds = None
        raise Exception(
            f"El archivo elegido como DEM tiene {ds.RasterCount if ds else '?'} bandas — "
            "un DEM de elevación real debe tener una sola banda. Parece que se seleccionó "
            "una ortofoto (RGB) en vez de un modelo de elevación."
        )

    gt = ds.GetGeoTransform()
    bbox = polygon_geom.boundingBox()

    def world_to_pixel(x, y):
        px = int((x - gt[0]) / gt[1])
        py = int((y - gt[3]) / gt[5])
        return px, py

    col0, row0 = world_to_pixel(bbox.xMinimum(), bbox.yMaximum())
    col1, row1 = world_to_pixel(bbox.xMaximum(), bbox.yMinimum())
    col0, col1 = sorted((max(0, col0), min(ds.RasterXSize, col1)))
    row0, row1 = sorted((max(0, row0), min(ds.RasterYSize, row1)))
    if col1 <= col0 or row1 <= row0:
        ds = None
        raise Exception("El polígono del predio no se traslapa con el DEM.")

    win_xsize = col1 - col0
    win_ysize = row1 - row0

    # Factor de reducción: si la ventana ya es chica, se lee tal cual (1:1).
    scale = min(1.0, max_pixels_per_side / max(win_xsize, win_ysize))
    buf_xsize = max(1, int(win_xsize * scale))
    buf_ysize = max(1, int(win_ysize * scale))

    band = ds.GetRasterBand(1)
    elev = band.ReadAsArray(col0, row0, win_xsize, win_ysize,
                             buf_xsize=buf_xsize, buf_ysize=buf_ysize).astype(np.float64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        elev = np.where(elev == nodata, np.nan, elev)

    # El tamaño de píxel efectivo cambia según cuánto se redujo la lectura.
    px_size_x = abs(gt[1]) * (win_xsize / buf_xsize)
    px_size_y = abs(gt[5]) * (win_ysize / buf_ysize)
    ds = None

    if elev.size == 0 or np.all(np.isnan(elev)):
        raise Exception("No hay datos válidos del DEM dentro del predio.")

    # Gradiente de elevación por diferencias finitas (dz/dfila, dz/dcolumna)
    dzdy, dzdx = np.gradient(elev, px_size_y, px_size_x)

    slope_pct = np.sqrt(dzdx ** 2 + dzdy ** 2) * 100.0
    # Aspecto = dirección de máxima pendiente DESCENDENTE, 0°=norte, horario.
    # Nota: el signo de dzdy se invierte porque las filas del array crecen
    # hacia el sur mientras que Y del mundo crece hacia el norte.
    aspect_deg = (np.degrees(np.arctan2(dzdx, -dzdy)) + 360.0) % 360.0

    valid = ~np.isnan(slope_pct)
    if not np.any(valid):
        raise Exception("No hay datos válidos del DEM dentro del predio.")

    mean_slope = float(np.nanmean(slope_pct[valid]))

    # Promedio circular del aspecto (un ángulo no se puede promediar directo:
    # 359° y 1° deben promediar a 0°, no a 180°).
    ang = np.radians(aspect_deg[valid])
    mean_aspect = math.degrees(math.atan2(float(np.mean(np.sin(ang))), float(np.mean(np.cos(ang))))) % 360.0

    return mean_slope, mean_aspect


def determine_orientation(dem_path, polygon_geom, slope_threshold_pct=12.0):
    """
    Decide el rumbo (bearing, grados desde el norte) de las líneas de siembra:
    - Pendiente media < umbral: Norte-Sur (bearing=0°), para máxima
      captación solar — criterio estándar de la industria en terreno plano.
    - Pendiente media >= umbral: perpendicular a la máxima pendiente (es
      decir, siguiendo las curvas de nivel), para controlar erosión y
      escorrentía en terreno inclinado.

    Retorna (bearing_deg, criterio_texto, pendiente_media_pct).
    """
    mean_slope, mean_aspect = compute_slope_and_aspect(dem_path, polygon_geom)

    if mean_slope < slope_threshold_pct:
        return 0.0, "Norte-Sur (terreno plano — prioriza captación solar)", mean_slope

    # Las curvas de nivel corren perpendiculares a la dirección de máxima
    # pendiente (el aspecto). Se normaliza a [0,180) porque una línea de
    # siembra no distingue "hacia adelante" de "hacia atrás".
    bearing = (mean_aspect + 90.0) % 180.0
    return bearing, "Curvas de nivel (pendiente pronunciada — control de erosión)", mean_slope


def _rotate(geom, angle_deg, center):
    g = QgsGeometry(geom)
    g.rotate(angle_deg, center)
    return g


def divide_property(polygon_geom, bearing_deg,
                     lot_width_m=150.0, max_length_m=350.0, max_area_ha=12.0,
                     main_road_width_m=8.0,
                     drainage_width_m=3.0, lots_per_block=2,
                     min_merge_fraction=0.4,
                     progress_callback=None):
    """
    Divide polygon_geom en lotes orientados según bearing_deg. El patrón entre
    lotes contiguos es: lote-drenaje-lote-drenaje-...-lote-VÍA-lote-drenaje-...
    — es decir, solo hay vía (con su drenaje al costado) cada `lots_per_block`
    lotes (borde de bloque); entre los demás lotes de un mismo bloque solo hay
    una franja de drenaje, SIN ninguna vía. Ninguna vía "secundaria": o hay
    vía principal, o no hay vía.

    lot_width_m es el ancho del lote (independiente del área — así se puede
    pedir un lote alargado); lot_length_m (=max_length_m) es el largo EN EL
    SENTIDO DE LAS LÍNEAS DE SIEMBRA, la distancia que camina un cosechero
    cortando el lote de punta a punta. max_area_ha es solo un TOPE de
    seguridad: si lot_width_m * max_length_m lo supera, se recorta el largo
    (nunca el ancho) para no pasarse.

    Los lotes de borde que quedan muy pequeños tras recortar contra la forma
    real del predio (menos de min_merge_fraction del área objetivo) se
    fusionan con el lote vecino más cercano DENTRO DE LA MISMA COLUMNA (nunca
    cruzando una vía, solo un drenaje).

    Retorna un dict:
        {
          "lotes": [(QgsGeometry, area_ha, bloque_id, lote_id_en_bloque), ...],
          "vias_principales": [QgsGeometry, ...],
          "drenajes": [QgsGeometry, ...],
        }
    Todas las geometrías quedan en el CRS/orientación real del predio (la
    rotación es solo un paso de trabajo interno).
    """
    center = polygon_geom.centroid().asPoint()

    # Rotar el predio para que las líneas de siembra queden "verticales" en el
    # espacio de trabajo: así dividir en lotes es una simple malla de
    # rectángulos, sin trigonometría en cada celda.
    rotated = _rotate(polygon_geom, -bearing_deg, center)
    bbox = rotated.boundingBox()

    lot_width = lot_width_m
    lot_length = max_length_m
    max_area_m2 = max_area_ha * M2_PER_HA
    if lot_width * lot_length > max_area_m2:
        lot_length = max_area_m2 / lot_width  # se recorta el largo, nunca el ancho
    target_area_m2 = lot_width * lot_length

    ancho_total = bbox.xMaximum() - bbox.xMinimum()
    columnas_estimadas = max(1, int(ancho_total / (lot_width + drainage_width_m)))

    columnas = []  # [(col_index, [QgsGeometry recortadas]), ...]
    vias_principales = []
    drenajes = []

    x = bbox.xMinimum()
    col_index = 0
    while x < bbox.xMaximum() - 1e-6:
        if col_index > 0:
            es_borde_bloque = (col_index % lots_per_block == 0)

            # Recortados contra el predio real (rotated), igual que los lotes:
            # sin esto, las vías/drenajes se dibujaban con el alto completo del
            # rectángulo envolvente y salían del polígono hacia zonas vacías.
            if es_borde_bloque:
                # Borde de bloque: SÍ hay vía principal, con su drenaje al costado.
                corridor = QgsGeometry.fromRect(
                    QgsRectangle(x, bbox.yMinimum(), x + main_road_width_m, bbox.yMaximum())
                )
                if corridor.intersects(rotated):
                    clipped = corridor.intersection(rotated)
                    if clipped and not clipped.isEmpty() and clipped.area() > 0:
                        vias_principales.append(_rotate(clipped, bearing_deg, center))
                x += main_road_width_m
            # Dentro de un mismo bloque: NUNCA hay vía entre lotes, solo drenaje.
            drain = QgsGeometry.fromRect(
                QgsRectangle(x, bbox.yMinimum(), x + drainage_width_m, bbox.yMaximum())
            )
            if drain.intersects(rotated):
                clipped = drain.intersection(rotated)
                if clipped and not clipped.isEmpty() and clipped.area() > 0:
                    drenajes.append(_rotate(clipped, bearing_deg, center))
            x += drainage_width_m

        col_width = min(lot_width, bbox.xMaximum() - x)
        if col_width <= 0.5:  # remanente de columna insignificante (< 0.5 m)
            break

        y = bbox.yMinimum()
        row_index = 0
        col_lotes = []
        while y < bbox.yMaximum() - 1e-6:
            if row_index > 0:
                drain = QgsGeometry.fromRect(
                    QgsRectangle(x, y, x + col_width, y + drainage_width_m)
                )
                if drain.intersects(rotated):
                    clipped = drain.intersection(rotated)
                    if clipped and not clipped.isEmpty() and clipped.area() > 0:
                        drenajes.append(_rotate(clipped, bearing_deg, center))
                y += drainage_width_m

            row_len = min(lot_length, bbox.yMaximum() - y)
            if row_len <= 0.5:
                break

            cell = QgsGeometry.fromRect(QgsRectangle(x, y, x + col_width, y + row_len))
            # Rechazo rápido en el espacio rotado (sin rotar la celda todavía):
            # en predios con forma dispersa o muy alargada, la mayoría de las
            # celdas de la rejilla caen fuera del polígono real, y probar
            # "intersects" es mucho más barato que calcular la intersección
            # completa para descartarlas.
            if cell.intersects(rotated):
                clipped_rotated = cell.intersection(rotated)
                if clipped_rotated and not clipped_rotated.isEmpty() and clipped_rotated.area() > 0:
                    col_lotes.append(_rotate(clipped_rotated, bearing_deg, center))

            y += row_len
            row_index += 1

        columnas.append((col_index, col_lotes))
        x += col_width
        col_index += 1

        if progress_callback:
            fraccion = min(0.95, col_index / max(col_index, columnas_estimadas))
            progress_callback(fraccion, f"Dividiendo predio... columna {col_index} de ~{columnas_estimadas}")

    # --- Fusionar remanentes de borde muy pequeños con el vecino de la misma
    # columna (nunca cruzando una vía, ya que las columnas están separadas
    # por vías/drenajes y el merge solo ocurre DENTRO de una columna) ---
    min_area_m2 = target_area_m2 * min_merge_fraction
    resultado_lotes = []
    bloque_id = 0
    for col_index, col_lotes in columnas:
        if col_index > 0 and col_index % lots_per_block == 0:
            bloque_id += 1
        if not col_lotes:
            continue

        fusionados = []
        i = 0
        while i < len(col_lotes):
            geom = col_lotes[i]
            area = geom.area()
            if area < min_area_m2 and fusionados:
                prev_geom, _ = fusionados[-1]
                nuevo = prev_geom.combine(geom)
                fusionados[-1] = (nuevo, nuevo.area())
            elif area < min_area_m2 and i + 1 < len(col_lotes):
                nxt = col_lotes[i + 1]
                nuevo = geom.combine(nxt)
                fusionados.append((nuevo, nuevo.area()))
                i += 1  # el siguiente ya quedó consumido en la fusión
            else:
                fusionados.append((geom, area))
            i += 1

        for lote_idx, (geom, area) in enumerate(fusionados):
            resultado_lotes.append((geom, area / M2_PER_HA, bloque_id, lote_idx))

    return {
        "lotes": resultado_lotes,
        "vias_principales": vias_principales,
        "drenajes": drenajes,
    }


def build_output_layers(division_result, crs_authid):
    """Empaqueta el resultado de divide_property() en capas de memoria de QGIS
    listas para agregar al proyecto: lotes (polígonos con atributos), vías
    (todas principales — ya no existe "secundaria") y drenajes."""

    lotes_layer = QgsVectorLayer(f"MultiPolygon?crs={crs_authid}", "Lotes diseñados", "memory")
    prov = lotes_layer.dataProvider()
    prov.addAttributes([
        QgsField("id", QVariant.Int),
        QgsField("bloque_id", QVariant.Int),
        QgsField("lote_id", QVariant.Int),
        QgsField("area_ha", QVariant.Double),
    ])
    lotes_layer.updateFields()
    feats = []
    for i, (geom, area_ha, bloque_id, lote_idx) in enumerate(division_result["lotes"], start=1):
        f = QgsFeature(lotes_layer.fields())
        f.setGeometry(geom)
        f.setAttributes([i, bloque_id, lote_idx, round(area_ha, 3)])
        feats.append(f)
    prov.addFeatures(feats)
    lotes_layer.updateExtents()

    vias_layer = QgsVectorLayer(f"MultiPolygon?crs={crs_authid}", "Vías", "memory")
    vp = vias_layer.dataProvider()
    vp.addAttributes([QgsField("id", QVariant.Int), QgsField("tipo", QVariant.String)])
    vias_layer.updateFields()
    vfeats = []
    contador = 1
    for g in division_result["vias_principales"]:
        f = QgsFeature(vias_layer.fields())
        f.setGeometry(g)
        f.setAttributes([contador, "principal"])
        vfeats.append(f)
        contador += 1
    vp.addFeatures(vfeats)
    vias_layer.updateExtents()

    drenajes_layer = QgsVectorLayer(f"MultiPolygon?crs={crs_authid}", "Drenajes", "memory")
    dp = drenajes_layer.dataProvider()
    dp.addAttributes([QgsField("id", QVariant.Int)])
    drenajes_layer.updateFields()
    dfeats = []
    for i, g in enumerate(division_result["drenajes"], start=1):
        f = QgsFeature(drenajes_layer.fields())
        f.setGeometry(g)
        f.setAttributes([i])
        dfeats.append(f)
    dp.addFeatures(dfeats)
    drenajes_layer.updateExtents()

    _style_layer(lotes_layer, "120,180,120,90", "60,110,60,255")     # verde translúcido
    _style_layer(vias_layer, "90,90,90,255", "40,40,40,255")          # gris oscuro sólido
    _style_layer(drenajes_layer, "80,150,220,220", "20,80,150,255")   # azul

    return lotes_layer, vias_layer, drenajes_layer


def _style_layer(layer, fill_rgba, outline_rgba):
    """Aplica un relleno/borde sencillo de un solo color — sin esto, las tres
    capas nuevas salen con el gris aleatorio por defecto de QGIS y a simple
    vista es imposible distinguir un lote de una vía o de un drenaje."""
    try:
        from qgis.core import QgsFillSymbol
        symbol = QgsFillSymbol.createSimple({
            "color": fill_rgba,
            "outline_color": outline_rgba,
            "outline_width": "0.4",
        })
        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()
    except Exception:
        # El estilo es cosmético: si falla, no debe tumbar la generación.
        logging.getLogger(__name__).debug(
            "No se pudo aplicar el estilo a la capa.", exc_info=True)
