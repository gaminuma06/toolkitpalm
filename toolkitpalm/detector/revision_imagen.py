# -*- coding: utf-8 -*-
"""
Revisión de las palmas muertas calculadas mirando la ortofoto.

Una posición sin palma detectada puede ser una erradicación real, una palma
que el detector no vio o una resiembra joven. Se distingue por el verdor
(índice ExG) en el punto, comparado con el suelo entre filas del mismo lote:
solo si es más verde que casi todo ese suelo (PERCENTIL_SUELO) queda como
"revisar"; si no, como muerta.

El criterio es conservador a propósito: medido sobre un lote real acierta
alrededor del 80% al separar palma de suelo, así que se prefiere marcar pocas
para revisar, con pocas falsas alarmas (1 de cada 20 puntos de suelo).

Al calibrarse con cada imagen no depende de umbrales fijos, que cambian con la
luz, la cámara o la época del vuelo.
"""
import logging
import math

import numpy
from osgeo import gdal, osr

from .numeracion_filas import MUERTA, REVISAR, VIVA

logger = logging.getLogger(__name__)

RADIO = 1.0            # m alrededor del punto
LADO_LECTURA = 24      # píxeles del lado de la ventana leída (se reduce al leer)
MUESTRAS_REFERENCIA = 300
DIFERENCIA_MINIMA = 0.01   # si palmas y suelo se parecen más que esto, no se decide
PERCENTIL_SUELO = 0.95     # verdor del suelo entre filas que hay que superar para "revisar"


class _Lector:
    def __init__(self, ruta, wkt_puntos):
        self.ds = gdal.Open(ruta)
        self.bandas = [self.ds.GetRasterBand(i) for i in (1, 2, 3)]
        self.gt = self.ds.GetGeoTransform()
        self.ancho, self.alto = self.ds.RasterXSize, self.ds.RasterYSize
        self.transformar = None
        wkt_raster = self.ds.GetProjection()
        if wkt_puntos and wkt_raster:
            origen, destino = osr.SpatialReference(), osr.SpatialReference()
            origen.ImportFromWkt(wkt_puntos)
            destino.ImportFromWkt(wkt_raster)
            for sr in (origen, destino):
                if hasattr(sr, "SetAxisMappingStrategy"):
                    sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if not origen.IsSame(destino):
                self.transformar = osr.CoordinateTransformation(origen, destino)

    def verdor(self, x, y):
        if self.transformar is not None:
            x, y = self.transformar.TransformPoint(x, y)[:2]
        gt = self.gt
        px, py = (x - gt[0]) / gt[1], (y - gt[3]) / gt[5]
        r = RADIO / abs(gt[1])
        x0, y0 = int(math.floor(px - r)), int(math.floor(py - r))
        x1, y1 = int(math.ceil(px + r)), int(math.ceil(py + r))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.ancho, x1), min(self.alto, y1)
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return None
        bw, bh = min(w, LADO_LECTURA), min(h, LADO_LECTURA)
        rgb = [b.ReadAsArray(x0, y0, w, h, buf_xsize=bw, buf_ysize=bh).astype(numpy.float64)
               for b in self.bandas]
        rojo, verde, azul = rgb
        suma = rojo + verde + azul
        validos = suma > 0
        if not validos.any():
            return None
        exg = (2 * verde - rojo - azul)[validos] / suma[validos]
        return float(exg.mean())


def _mediana(valores):
    v = sorted(x for x in valores if x is not None)
    return v[len(v) // 2] if v else None


def revisar_muertas(filas, ruta_ortofoto, wkt_puntos, ux, uy, sep):
    """Marca como REVISAR las muertas que en la imagen parecen una palma viva.

    filas: listas de (x, y, estado, origen). Devuelve (filas, cantidad_revisar).
    Si la imagen no se puede leer o no permite distinguir, no cambia nada.
    """
    try:
        lector = _Lector(ruta_ortofoto, wkt_puntos)
        if lector.ds is None or lector.ds.RasterCount < 3:
            return filas, 0
    except Exception:
        logger.debug("No se pudo abrir la ortofoto para revisar", exc_info=True)
        return filas, 0

    vivas = [(p[0], p[1]) for f in filas for p in f if p[2] == VIVA]
    if not vivas:
        return filas, 0
    cada = max(1, len(vivas) // MUESTRAS_REFERENCIA)
    muestra = vivas[::cada]
    px, py = -uy, ux
    palma = [v for v in (lector.verdor(x, y) for x, y in muestra) if v is not None]
    suelo = sorted(v for v in (lector.verdor(x + px * sep / 2.0, y + py * sep / 2.0)
                               for x, y in muestra) if v is not None)
    if not palma or not suelo or _mediana(palma) - _mediana(suelo) < DIFERENCIA_MINIMA:
        logger.info("La imagen no permite distinguir palma de suelo: no se revisan muertas")
        return filas, 0
    umbral = suelo[int(PERCENTIL_SUELO * (len(suelo) - 1))]
    logger.info(f"Verdor: palmas {_mediana(palma):.3f}, suelo {_mediana(suelo):.3f}, "
                f"umbral para revisar {umbral:.3f}")

    revisar = 0
    resultado = []
    for fila in filas:
        nueva = []
        for x, y, estado, origen in fila:
            if estado == MUERTA:
                v = lector.verdor(x, y)
                if v is not None and v >= umbral:
                    estado = REVISAR
                    revisar += 1
            nueva.append((x, y, estado, origen))
        resultado.append(nueva)
    return resultado, revisar
