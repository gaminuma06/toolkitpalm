# -*- coding: utf-8 -*-
"""
Lectura de la tabla del usuario (Excel, CSV u ODS) y detección automática de
las columnas de coordenadas, del sistema de coordenadas y del tipo de cada
columna. Solo usa GDAL/OGR, que viene con cualquier QGIS.
"""
import logging
import math
import os
import re
import unicodedata

from osgeo import gdal, ogr

gdal.UseExceptions()
ogr.UseExceptions()

# ── Lectura ──────────────────────────────────────────────────────────────────

_ENCABEZADOS = ("OGR_XLSX_HEADERS", "OGR_XLS_HEADERS", "OGR_ODS_HEADERS")


def hojas(ruta):
    """Nombres de las hojas (en un CSV, una sola)."""
    ds = _abrir(ruta)
    return [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())]


def _abrir(ruta):
    if os.path.splitext(ruta)[1].lower() in (".csv", ".txt"):
        return gdal.OpenEx(ruta, gdal.OF_VECTOR,
                           open_options=["AUTODETECT_TYPE=YES", "HEADERS=YES"])
    for opcion in _ENCABEZADOS:
        gdal.SetConfigOption(opcion, "FORCE")   # la primera fila son los nombres
    try:
        return gdal.OpenEx(ruta, gdal.OF_VECTOR)
    finally:
        for opcion in _ENCABEZADOS:
            gdal.SetConfigOption(opcion, None)


def leer_tabla(ruta, hoja=None):
    """Devuelve (columnas, filas): filas es una lista de dicts columna -> valor."""
    ds = _abrir(ruta)
    capa = ds.GetLayerByName(hoja) if hoja else ds.GetLayer(0)
    definicion = capa.GetLayerDefn()
    columnas = [definicion.GetFieldDefn(i).GetName() for i in range(definicion.GetFieldCount())]
    filas = []
    for f in capa:
        fila = {c: f.GetField(i) for i, c in enumerate(columnas)}
        if any(v not in (None, "") for v in fila.values()):
            filas.append(fila)
    return columnas, filas


# ── Valores ──────────────────────────────────────────────────────────────────

def normalizar(texto):
    """Minúsculas, sin tildes y sin signos: 'Media-Alta ' -> 'media alta'."""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())


def a_numero(valor):
    """Número o None. Acepta '4,25' y '1.234,5'."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor) if math.isfinite(valor) else None
    t = str(valor).strip().replace(" ", "")
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif t.count(",") == 1:
        t = t.replace(",", ".")
    try:
        n = float(t)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


# ── Tipo de cada columna ─────────────────────────────────────────────────────

NUMERICA, CATEGORICA, OTRA = "numerica", "categorica", "otra"
MAX_CATEGORIAS = 30


def tipo_de_columna(filas, columna):
    valores = [f[columna] for f in filas if f.get(columna) not in (None, "")]
    if not valores:
        return OTRA
    numeros = sum(1 for v in valores if a_numero(v) is not None)
    if numeros >= 0.9 * len(valores):
        return NUMERICA
    distintos = {normalizar(v) for v in valores}
    return CATEGORICA if len(distintos) <= MAX_CATEGORIAS else OTRA


# ── Coordenadas ──────────────────────────────────────────────────────────────

_NOMBRES_X = {"x", "este", "east", "easting", "lon", "long", "lng", "longitud", "longitude",
              "coordx", "coord x", "coordenada x", "x m", "xcoord", "utm x", "utmx", "e"}
_NOMBRES_Y = {"y", "norte", "north", "northing", "lat", "latitud", "latitude",
              "coordy", "coord y", "coordenada y", "y m", "ycoord", "utm y", "utmy", "n"}


def _puntaje_nombre(columna, nombres):
    n = normalizar(columna)
    if n in nombres:
        return 3
    if any(re.search(rf"\b{re.escape(c)}\b", n) for c in nombres if len(c) > 1):
        return 2
    return 0


def detectar_xy(columnas, filas):
    """(columna_x, columna_y) más probables, o (None, None)."""
    numericas = [c for c in columnas if tipo_de_columna(filas, c) == NUMERICA]
    if len(numericas) < 2:
        return None, None
    mejor_x = max(numericas, key=lambda c: _puntaje_nombre(c, _NOMBRES_X))
    mejor_y = max([c for c in numericas if c != mejor_x],
                  key=lambda c: _puntaje_nombre(c, _NOMBRES_Y))
    if _puntaje_nombre(mejor_x, _NOMBRES_X) and _puntaje_nombre(mejor_y, _NOMBRES_Y):
        return mejor_x, mejor_y
    # Sin nombres reconocibles: el primer par de columnas con valores de coordenada.
    for i, a in enumerate(numericas):
        for b in numericas[i + 1:]:
            xs = [a_numero(f[a]) for f in filas if a_numero(f.get(a)) is not None]
            ys = [a_numero(f[b]) for f in filas if a_numero(f.get(b)) is not None]
            if xs and ys and _parecen_coordenadas(xs, ys):
                return a, b
    return None, None


def _parecen_coordenadas(xs, ys):
    geograficas = all(abs(x) <= 180 for x in xs) and all(abs(y) <= 90 for y in ys)
    planas = min(map(abs, xs)) > 1000 and min(map(abs, ys)) > 1000
    dispersas = (max(xs) - min(xs)) > 0 and (max(ys) - min(ys)) > 0
    return dispersas and (geograficas or planas)


# Sistemas que se prueban, en orden de preferencia después del del proyecto.
CANDIDATOS_CRS = ("EPSG:9377", "EPSG:3116", "EPSG:3115", "EPSG:3117", "EPSG:3114",
                  "EPSG:3118", "EPSG:32618", "EPSG:32617", "EPSG:32619", "EPSG:4326")


def coordenadas(filas, cx, cy):
    """Listas (xs, ys, indices_validos) de las filas con ambas coordenadas."""
    xs, ys, idx = [], [], []
    for i, f in enumerate(filas):
        x, y = a_numero(f.get(cx)), a_numero(f.get(cy))
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
            idx.append(i)
    return xs, ys, idx


def detectar_crs(xs, ys, crs_proyecto=None):
    """Sistemas de coordenadas plausibles, del más al menos probable.

    Un sistema es plausible si, al llevar el centro de los puntos a latitud y
    longitud, queda dentro de la zona de uso oficial de ese sistema. Se prefiere
    el del proyecto si es plausible.
    """
    from osgeo import osr
    if not xs:
        return []
    cx, cy = sorted(xs)[len(xs) // 2], sorted(ys)[len(ys) // 2]
    geografico = osr.SpatialReference()
    geografico.ImportFromEPSG(4326)
    geografico.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    candidatos = ([crs_proyecto] if crs_proyecto else []) + [
        c for c in CANDIDATOS_CRS if c != crs_proyecto]
    plausibles = []
    for authid in candidatos:
        try:
            srs = osr.SpatialReference()
            if srs.SetFromUserInput(authid) != 0:
                continue
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if srs.IsGeographic():
                lon, lat = cx, cy
                if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    continue
            else:
                lon, lat = osr.CoordinateTransformation(srs, geografico).TransformPoint(cx, cy)[:2]
            area = srs.GetAreaOfUse()
            if area is not None:
                margen = 1.0
                if not (area.west_lon_degree - margen <= lon <= area.east_lon_degree + margen
                        and area.south_lat_degree - margen <= lat <= area.north_lat_degree + margen):
                    continue
            plausibles.append(authid)
        except Exception:
            logging.getLogger(__name__).debug("Sistema %s no aplicable", authid, exc_info=True)
    return plausibles
