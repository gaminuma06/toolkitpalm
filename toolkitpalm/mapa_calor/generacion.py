# -*- coding: utf-8 -*-
"""
Generación del mapa: capa de puntos, área para delimitar, zonas de Thiessen y
superficie interpolada (IDW), con sus colores por rango.
"""
import logging
import math
import os
import tempfile

import numpy
from osgeo import gdal, ogr, osr
from qgis.core import (QgsCategorizedSymbolRenderer, QgsColorRampShader, QgsCoordinateTransform,
                       QgsFeature, QgsField, QgsFillSymbol, QgsGeometry, QgsGraduatedSymbolRenderer,
                       QgsMarkerSymbol, QgsPointXY, QgsProject, QgsRasterLayer, QgsRasterShader,
                       QgsRendererCategory, QgsRendererRange, QgsSingleBandPseudoColorRenderer,
                       QgsSingleSymbolRenderer, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor

from . import lectura

gdal.UseExceptions()
logger = logging.getLogger(__name__)

LADO_RASTER = 500          # celdas del lado mayor de la superficie interpolada
POTENCIA_IDW = 2.0
MAX_PUNTOS_TODOS = 500     # con más puntos, cada celda usa solo sus vecinos cercanos
CIERRE_LINDERO_M = 15.0    # une lotes separados por vías o drenes de hasta ~30 m


# ── Puntos ───────────────────────────────────────────────────────────────────

def capa_de_puntos(columnas, filas, cx, cy, authid, nombre):
    """Capa en memoria con un punto por fila y todas las columnas del Excel.
    Devuelve (capa, filas_sin_coordenadas)."""
    tipos = {c: lectura.tipo_de_columna(filas, c) for c in columnas}
    capa = QgsVectorLayer(f"Point?crs={authid}", nombre, "memory")
    campos = [QgsField(c, QVariant.Double if tipos[c] == lectura.NUMERICA else QVariant.String)
              for c in columnas]
    capa.dataProvider().addAttributes(campos)
    capa.updateFields()
    entidades, sin = [], 0
    for fila in filas:
        x, y = lectura.a_numero(fila.get(cx)), lectura.a_numero(fila.get(cy))
        if x is None or y is None:
            sin += 1
            continue
        f = QgsFeature(capa.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        for c in columnas:
            v = fila.get(c)
            f[c] = lectura.a_numero(v) if tipos[c] == lectura.NUMERICA else (
                None if v in (None, "") else str(v))
        entidades.append(f)
    capa.dataProvider().addFeatures(entidades)
    capa.updateExtents()
    return capa, sin


# ── Área para delimitar ──────────────────────────────────────────────────────

def tiene_varios_poligonos(capa, solo_seleccion=False):
    entidades = list(capa.getSelectedFeatures() if solo_seleccion else capa.getFeatures())
    if len(entidades) > 1:
        return True
    return bool(entidades) and entidades[0].geometry().isMultipart() and \
        len(entidades[0].geometry().asGeometryCollection()) > 1


def mascara(capa, crs_destino, modo="lotes", solo_seleccion=False):
    """Geometría para delimitar el mapa, en el sistema de los puntos.

    modo 'lotes': el área de los polígonos tal cual (quedan fuera las vías y
    espacios entre lotes). modo 'lindero': el contorno exterior de todo el
    conjunto, uniendo lotes separados por vías angostas y sin huecos internos."""
    entidades = list(capa.getSelectedFeatures() if solo_seleccion else capa.getFeatures())
    if not entidades:
        return None
    xf = None
    if capa.crs() != crs_destino:
        xf = QgsCoordinateTransform(capa.crs(), crs_destino, QgsProject.instance())
    geoms = []
    for f in entidades:
        g = QgsGeometry(f.geometry())
        if xf is not None:
            g.transform(xf)
        if not g.isEmpty():
            geoms.append(g.makeValid())
    if not geoms:
        return None
    union = QgsGeometry.unaryUnion(geoms)
    if modo == "lindero":
        d = CIERRE_LINDERO_M if not crs_destino.isGeographic() else CIERRE_LINDERO_M / 111000.0
        union = union.buffer(d, 8).buffer(-d, 8)
        partes = union.asGeometryCollection() if union.isMultipart() else [union]
        exteriores = []
        for p in partes:
            poligono = p.asPolygon()
            if poligono:
                exteriores.append(QgsGeometry.fromPolygonXY([poligono[0]]))
        union = QgsGeometry.unaryUnion(exteriores) if exteriores else union
    return union


def envolvente_de_puntos(capa):
    """Área cubierta por los puntos (envolvente convexa), para no extrapolar lejos."""
    geoms = [f.geometry() for f in capa.getFeatures()]
    return QgsGeometry.unaryUnion(geoms).convexHull() if geoms else None


def _capa_de_geometria(geom, authid, nombre):
    capa = QgsVectorLayer(f"Polygon?crs={authid}", nombre, "memory")
    f = QgsFeature()
    f.setGeometry(geom)
    capa.dataProvider().addFeatures([f])
    return capa


# ── Thiessen ─────────────────────────────────────────────────────────────────

def zonas_thiessen(puntos, area, nombre):
    """Una zona por punto (el área más cercana a él), recortada a `area`."""
    import processing
    authid = puntos.crs().authid()
    voronoi = processing.run("native:voronoipolygons", {
        "INPUT": puntos, "BUFFER": 30, "OUTPUT": "memory:"})["OUTPUT"]
    recorte = processing.run("native:clip", {
        "INPUT": voronoi, "OVERLAY": _capa_de_geometria(area, authid, "area"),
        "OUTPUT": "memory:"})["OUTPUT"]
    recorte.setName(nombre)
    return recorte


# ── Superficie interpolada ───────────────────────────────────────────────────

def superficie_idw(puntos, campo_valor, valores_por_fid, area, ruta_salida):
    """Superficie IDW sobre `area`, con lo de afuera transparente. Devuelve la ruta."""
    wkt = puntos.crs().toWkt()
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)

    # gdal.Grid pide un dataset de GDAL (no un DataSource de OGR).
    mem = gdal.GetDriverByName("Memory").Create("", 0, 0, 0, gdal.GDT_Unknown)
    capa = mem.CreateLayer("p", srs=srs, geom_type=ogr.wkbPoint)
    capa.CreateField(ogr.FieldDefn("valor", ogr.OFTReal))
    n = 0
    for f in puntos.getFeatures():
        v = valores_por_fid.get(f.id())
        if v is None:
            continue
        p = f.geometry().asPoint()
        o = ogr.Feature(capa.GetLayerDefn())
        o.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({p.x()} {p.y()})"))
        o.SetField("valor", float(v))
        capa.CreateFeature(o)
        n += 1
    if n < 3:
        raise ValueError("Se necesitan al menos 3 puntos con valor para interpolar.")

    caja = area.boundingBox()
    ancho_m, alto_m = caja.width(), caja.height()
    celda = max(ancho_m, alto_m) / float(LADO_RASTER)
    w, h = max(1, int(math.ceil(ancho_m / celda))), max(1, int(math.ceil(alto_m / celda)))
    xmin, ymax = caja.xMinimum(), caja.yMaximum()

    # Con muchos puntos cada celda se calcula solo con sus vecinos cercanos: con
    # todos, el tiempo crece con puntos x celdas y se vuelve lento.
    if n > MAX_PUNTOS_TODOS:
        radio = 6.0 * math.sqrt(max(area.area(), 1.0) / n)
        algoritmo = f"invdistnn:power={POTENCIA_IDW}:radius={radio}:max_points=16:nodata=-9999"
    else:
        algoritmo = f"invdist:power={POTENCIA_IDW}:smoothing=0"
    intermedio = os.path.join(tempfile.mkdtemp(prefix="tkp_calor_"), "idw.tif")
    gdal.Grid(intermedio, mem, options=gdal.GridOptions(
        format="GTiff", algorithm=algoritmo,
        zfield="valor", outputBounds=[xmin, ymax, xmin + w * celda, ymax - h * celda],
        width=w, height=h, outputType=gdal.GDT_Float32))

    # Fuera del área: sin datos.
    ds = gdal.Open(intermedio)
    datos = ds.GetRasterBand(1).ReadAsArray().astype(numpy.float32)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    ds = None
    mascara_ds = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
    mascara_ds.SetGeoTransform(gt)
    mascara_ds.SetProjection(proj)
    vec = ogr.GetDriverByName("Memory").CreateDataSource("")
    lv = vec.CreateLayer("m", srs=srs, geom_type=ogr.wkbMultiPolygon)
    of = ogr.Feature(lv.GetLayerDefn())
    of.SetGeometry(ogr.CreateGeometryFromWkt(area.asWkt()))
    lv.CreateFeature(of)
    gdal.RasterizeLayer(mascara_ds, [1], lv, burn_values=[1])
    dentro = mascara_ds.GetRasterBand(1).ReadAsArray() > 0
    NODATA = -9999.0
    datos[~dentro] = NODATA
    datos[datos <= -9998] = NODATA

    salida = gdal.GetDriverByName("GTiff").Create(
        ruta_salida, w, h, 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE", "TILED=YES"])
    salida.SetGeoTransform(gt)
    salida.SetProjection(proj)
    banda = salida.GetRasterBand(1)
    banda.WriteArray(datos)
    banda.SetNoDataValue(NODATA)
    salida = None
    return ruta_salida


# ── Estilos ──────────────────────────────────────────────────────────────────

def estilo_rangos_vector(capa, campo, limites, colores, etiquetas, minimo, maximo):
    bordes = [minimo] + list(limites) + [maximo]
    rangos = []
    for i, color in enumerate(colores):
        simbolo = QgsFillSymbol.createSimple({"color": color, "outline_color": "#555555",
                                              "outline_width": "0.1"})
        rangos.append(QgsRendererRange(bordes[i], bordes[i + 1], simbolo, etiquetas[i]))
    capa.setRenderer(QgsGraduatedSymbolRenderer(campo, rangos))


def estilo_categorias_vector(capa, campo, categorias, colores):
    cats = []
    for valor, color in zip(categorias, colores):
        simbolo = QgsFillSymbol.createSimple({"color": color, "outline_color": "#555555",
                                              "outline_width": "0.1"})
        cats.append(QgsRendererCategory(valor, simbolo, str(valor)))
    capa.setRenderer(QgsCategorizedSymbolRenderer(campo, cats))


def estilo_raster_clases(capa, limites_superiores, colores, etiquetas):
    """Clases discretas: cada valor toma el color de la primera clase cuyo límite
    superior lo alcanza (el último límite es infinito)."""
    funcion = QgsColorRampShader()
    funcion.setColorRampType(QgsColorRampShader.Discrete)
    items = [QgsColorRampShader.ColorRampItem(lim, QColor(c), et)
             for lim, c, et in zip(limites_superiores, colores, etiquetas)]
    funcion.setColorRampItemList(items)
    sombreador = QgsRasterShader()
    sombreador.setRasterShaderFunction(funcion)
    capa.setRenderer(QgsSingleBandPseudoColorRenderer(capa.dataProvider(), 1, sombreador))


def estilo_puntos(capa):
    simbolo = QgsMarkerSymbol.createSimple({"name": "circle", "color": "#222222", "size": "1.6",
                                            "outline_color": "#ffffff", "outline_width": "0.3"})
    capa.setRenderer(QgsSingleSymbolRenderer(simbolo))
