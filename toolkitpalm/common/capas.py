# -*- coding: utf-8 -*-
"""Búsqueda de la capa de lotes cargada en el proyecto, compartida por las herramientas."""
import os

from qgis.core import QgsProject, QgsVectorLayer, QgsWkbTypes

# Un shapefile son varios archivos con el mismo nombre; QGIS abre la capa desde
# cualquiera de ellos, pero el que la representa es el .shp.
COMPLEMENTOS_SHAPEFILE = ('.shx', '.dbf', '.prj', '.cpg', '.qpj', '.sbn', '.sbx', '.qix', '.qmd')


def ruta_shp(ruta):
    """Si `ruta` es un complemento de un shapefile (.shx, .dbf…), devuelve su .shp."""
    if not ruta:
        return ruta
    base, ext = os.path.splitext(ruta.strip())
    if ext.lower() in COMPLEMENTOS_SHAPEFILE:
        for candidata in (base + '.shp', base + '.SHP'):
            if os.path.exists(candidata):
                return candidata
    return ruta.strip()


def fuente_de_capa(capa):
    """Ruta de archivo de una capa, sin prefijos de URI ni opciones de QGIS."""
    s = (capa.source() or '').split('|')[0].strip()
    for prefijo in ('file:///', 'file://', 'file:'):
        if s.lower().startswith(prefijo):
            s = s[len(prefijo):]
            break
    return ruta_shp(os.path.normpath(s)) if s else s


def _igual(a, b):
    return os.path.normcase(os.path.normpath(os.path.abspath(a))) == \
        os.path.normcase(os.path.normpath(os.path.abspath(b)))


def buscar_capa_lotes(ruta):
    """Devuelve (capa, ruta_shp_de_la_capa, sustituida).

    Primero busca la capa cuyo archivo es `ruta`. Si no está cargada, usa la capa
    de polígonos que tenga un lote seleccionado, o la única capa de polígonos del
    proyecto; en ese caso `sustituida` es True y quien llama debe usar la ruta
    devuelta (no la original), para no leer el lote de otro archivo.
    """
    capas = [c for c in QgsProject.instance().mapLayers().values()
             if isinstance(c, QgsVectorLayer) and c.providerType() == 'ogr']

    if ruta:
        objetivo = ruta_shp(ruta)
        for capa in capas:
            fuente = fuente_de_capa(capa)
            if fuente and _igual(fuente, objetivo):
                return capa, fuente, False

    poligonos = [c for c in capas
                 if c.geometryType() == QgsWkbTypes.PolygonGeometry
                 and fuente_de_capa(c).lower().endswith('.shp')]
    con_seleccion = [c for c in poligonos if c.selectedFeatureCount() > 0]
    if len(con_seleccion) == 1:
        elegida = con_seleccion[0]
    elif len(poligonos) == 1:
        elegida = poligonos[0]
    else:
        return None, None, False
    return elegida, fuente_de_capa(elegida), True
