# -*- coding: utf-8 -*-
"""
Lógica del Optimizador de Acopios reutilizable sin el panel abierto (headless),
pensada para invocarse desde el Asistente (chat) o cualquier automatización.

El flujo con UI (OptimizadorAcopiosDockWidget._on_optimize) no se modifica en este
archivo — construye su propia versión de estos mismos pasos con progreso visual y
estilos/orden de capas más elaborados. Aquí se prioriza un flujo simple y directo.
"""

import os
import tempfile
import zipfile
import logging
from datetime import datetime

import requests
from qgis.core import QgsProject, QgsVectorLayer, QgsVectorFileWriter, QgsField, QgsWkbTypes

from ..config import OPTIMIZE_ENDPOINT, API_KEY, REQUESTS_TIMEOUT, DEFAULT_P_ACOPIOS, DEFAULT_ROAD_INTERVAL_M

logger = logging.getLogger(__name__)


def sanitize_fid_field(layer):
    """Renombra un campo 'fid' de atributo (choca con la columna interna de GeoPackage)."""
    fid_idx = layer.fields().lookupField("fid")
    if fid_idx < 0:
        return layer

    mem_layer = QgsVectorLayer(
        f"{QgsWkbTypes.displayString(layer.wkbType())}?crs={layer.crs().authid()}",
        layer.name(),
        "memory",
    )
    mem_provider = mem_layer.dataProvider()

    new_fields = []
    for field in layer.fields():
        if field.name().lower() == "fid":
            renamed = QgsField(field)
            renamed.setName("fid_origen")
            new_fields.append(renamed)
        else:
            new_fields.append(field)
    mem_provider.addAttributes(new_fields)
    mem_layer.updateFields()

    mem_provider.addFeatures(list(layer.getFeatures()))
    mem_layer.updateExtents()
    return mem_layer


def layer_to_temp_gpkg(layer, prefix):
    """Exporta una capa a un archivo GPKG temporal. Retorna la ruta."""
    export_layer = sanitize_fid_field(layer)
    fd, path = tempfile.mkstemp(suffix=".gpkg", prefix=prefix)
    os.close(fd)
    err_code, err_msg = QgsVectorFileWriter.writeAsVectorFormat(
        export_layer,
        path,
        "UTF-8",
        export_layer.crs(),
        "GPKG",
    )
    if err_code != QgsVectorFileWriter.NoError:
        try:
            os.remove(path)
        except Exception:
            pass
        raise RuntimeError(err_msg or "Error exportando capa a GPKG")
    return path


def output_dir_from_layer(layer):
    """Carpeta base para resultados: mismo directorio que la capa de origen, si existe."""
    try:
        src = layer.source()
        if src:
            path = src.split("|")[0].split("?")[0].strip()
            if path.startswith("file://"):
                path = path[7:]
            dir_path = os.path.dirname(path)
            if dir_path and os.path.isdir(dir_path):
                return dir_path
    except Exception:
        pass
    try:
        project_path = QgsProject.instance().fileName()
        if project_path:
            return os.path.dirname(project_path)
    except Exception:
        pass
    return os.path.expanduser("~")


def find_layer_by_name(layer_name):
    """Busca la primera capa del proyecto cuyo nombre coincide (sensible a mayúsculas)."""
    for layer in QgsProject.instance().mapLayers().values():
        if layer.name() == layer_name:
            return layer
    return None


def run_optimizador_headless(lots_layer_name, roads_layer_name, acopios_layer_name=None,
                              p=None, road_interval=None, yield_col=None, price_col=None):
    """
    Ejecuta el Optimizador de Acopios sobre capas ya cargadas en el proyecto de QGIS,
    identificadas por nombre. No requiere que el panel del Optimizador esté abierto.

    Retorna dict: {"layer_name", "results_dir", "report_text"}.
    """
    if not API_KEY:
        raise Exception("No está configurada la X-API-Key. Configure config_secrets.py o la variable OPTIMIZADOR_ACOPIOS_API_KEY.")

    lots_layer = find_layer_by_name(lots_layer_name)
    if lots_layer is None:
        raise Exception(f"No se encontró la capa de lotes '{lots_layer_name}' en el proyecto")
    roads_layer = find_layer_by_name(roads_layer_name)
    if roads_layer is None:
        raise Exception(f"No se encontró la capa de carreteras '{roads_layer_name}' en el proyecto")
    acopios_layer = find_layer_by_name(acopios_layer_name) if acopios_layer_name else None

    p_val = p if p is not None else DEFAULT_P_ACOPIOS
    interval_val = road_interval if road_interval is not None else DEFAULT_ROAD_INTERVAL_M

    tmp_dir = tempfile.mkdtemp(prefix="optimizador_acopios_")
    lots_path = layer_to_temp_gpkg(lots_layer, "lots_")
    roads_path = layer_to_temp_gpkg(roads_layer, "roads_")
    acopios_path = layer_to_temp_gpkg(acopios_layer, "acopios_") if acopios_layer else None

    files = {
        "lots": ("lotes.gpkg", open(lots_path, "rb"), "application/octet-stream"),
        "roads": ("carreteras.gpkg", open(roads_path, "rb"), "application/octet-stream"),
    }
    if acopios_path:
        files["current_acopios"] = ("acopios.gpkg", open(acopios_path, "rb"), "application/octet-stream")

    data = {
        "p": p_val,
        "road_interval": interval_val,
        "target_crs": "EPSG:3116",
        "time_limit": 600,
        "gap_rel": 0.0,
    }
    if yield_col:
        data["yield_col"] = yield_col
    else:
        data["no_yield_col"] = "1"
    if price_col:
        data["price_col"] = price_col
    else:
        data["no_price_col"] = "1"

    from ..client_identity import cabeceras_autenticacion
    headers = cabeceras_autenticacion()
    logger.info(f"Enviando optimización: lotes={lots_layer_name}, carreteras={roads_layer_name}, p={p_val}")
    try:
        response = requests.post(OPTIMIZE_ENDPOINT, files=files, data=data, headers=headers, timeout=REQUESTS_TIMEOUT)
    finally:
        for fh in files.values():
            if hasattr(fh[1], "close"):
                fh[1].close()
        for path in (lots_path, roads_path) + ((acopios_path,) if acopios_path else ()):
            try:
                os.remove(path)
            except Exception:
                pass
        try:
            os.rmdir(tmp_dir)
        except Exception:
            pass

    if response.status_code == 401:
        raise Exception("X-API-Key inválida o ausente. Verifique la configuración del plugin.")
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text or f"Código {response.status_code}"
        raise Exception(f"La API respondió con error: {detail}")

    fd_zip, zip_path = tempfile.mkstemp(suffix=".zip", prefix="optimizador_acopios_")
    os.close(fd_zip)
    with open(zip_path, "wb") as f:
        f.write(response.content)

    base_dir = output_dir_from_layer(lots_layer)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_dir = os.path.join(base_dir, f"resultados_optimizador_{timestamp}")
    os.makedirs(results_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(results_dir)
    try:
        os.remove(zip_path)
    except Exception:
        pass

    opt_points_path = os.path.join(results_dir, "optimal_collection_points.gpkg")
    layer_name = None
    if os.path.isfile(opt_points_path):
        layer_opt = QgsVectorLayer(opt_points_path, "Acopios óptimos", "ogr")
        if layer_opt.isValid():
            QgsProject.instance().addMapLayer(layer_opt)
            layer_name = layer_opt.name()

    report_path = os.path.join(results_dir, "optimization_report.txt")
    report_text = ""
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report_text = f.read()
        except Exception:
            pass

    logger.info(f"Optimización completada. Resultados en: {results_dir}")

    return {
        "layer_name": layer_name,
        "results_dir": results_dir,
        "report_text": report_text,
    }
