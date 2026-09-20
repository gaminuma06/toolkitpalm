# -*- coding: utf-8 -*-
import os
import requests
import tempfile
import zipfile
import shutil
import threading
import time
import uuid
from qgis.PyQt.QtCore import QSettings, QCoreApplication, Qt, QTranslator, QTimer, QThread, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (QAction, QMessageBox, QApplication,
                                QLabel)
from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer, QgsProcessingFeedback, QgsProcessingUtils, QgsApplication, QgsProcessingContext, QgsCoordinateReferenceSystem, QgsCoordinateTransform
from .dockwidget import ProgressDialog
import logging
import sys
import traceback
from datetime import datetime

# Importar configuración (nombres genéricos aliasados desde el config.py unificado)
from ..config import (API_BASE_URL, SEGMENT_PALMS_ENDPOINT, SEGMENT_PALMS_ASYNC_ENDPOINT,
                    SEGMENTADOR_STATUS_ENDPOINT as STATUS_ENDPOINT,
                    SEGMENTADOR_RESULT_ENDPOINT as RESULT_ENDPOINT,
                    SEGMENTADOR_PROGRESS_ENDPOINT as PROGRESS_ENDPOINT,
                    UPLOAD_METHOD, UPLOAD_TIMEOUT, UPLOAD_PROGRESS_INTERVAL, API_KEY,
                    SEGMENTADOR_QUEUE_POLLING_INTERVAL as QUEUE_POLLING_INTERVAL,
                    SEGMENTADOR_QUEUE_TIMEOUT as QUEUE_TIMEOUT,
                    DEFAULT_SLICE_HEIGHT, DEFAULT_SLICE_WIDTH, DEFAULT_OVERLAP_RATIO,
                    DEFAULT_CONFIDENCE_THRESHOLD, DEFAULT_STANDARD_RESOLUTION, DEFAULT_RES_THRESHOLD,
                    RESAMPLE_CLIP_TARGET_M_PX)


def _get_raster_dimensions(raster_path):
    """Obtiene ancho y alto del raster en píxeles. Retorna (width, height) o (None, None)."""
    try:
        from osgeo import gdal
        ds = gdal.Open(raster_path)
        if ds:
            w, h = ds.RasterXSize, ds.RasterYSize
            ds = None
            return w, h
    except Exception:
        logging.getLogger(__name__).debug(
            "Fallo no crítico; se continúa.", exc_info=True)
    try:
        import rasterio
        with rasterio.open(raster_path) as src:
            return src.width, src.height
    except Exception:
        logging.getLogger(__name__).debug(
            "Fallo no crítico; se continúa.", exc_info=True)
    return None, None


def _resample_clip_to_resolution(raster_path, target_resolution_m, progress_callback=None):
    """
    Remuestrea el raster (p. ej. el resultado del clip) a target_resolution_m m/píxel.
    Solo en el plugin, antes de enviar a la API: reduce tamaño del TIFF y tiempo de proceso.
    Usa GDAL (siempre disponible dentro de QGIS, a diferencia de rasterio, que no viene
    incluido en el Python de QGIS por defecto y antes hacía que este paso se saltara en
    silencio, subiendo el clip a resolución nativa del dron y superando el límite de
    tamaño de request del backend).
    Retorna la ruta del archivo remuestreado o raster_path si falla (se usa el original).
    """
    if target_resolution_m is None or target_resolution_m <= 0:
        return raster_path
    try:
        from osgeo import gdal
    except ImportError as e:
        logger.warning(f"GDAL no disponible; no se remuestrea el clip: {e}")
        return raster_path
    out_dir = os.path.dirname(raster_path)
    out_path = os.path.join(out_dir, "clip_resampled_10cm.tif")
    try:
        if progress_callback:
            progress_callback(12, "Remuestreando clip a 10 cm/píxel...")
            QApplication.processEvents()
        src_ds = gdal.Open(raster_path)
        if src_ds is None:
            return raster_path
        gt = src_ds.GetGeoTransform()
        res_x, res_y = abs(gt[1]), abs(gt[5])
        src_ds = None
        if res_x <= 0 or res_y <= 0:
            return raster_path
        warp_options = gdal.WarpOptions(
            xRes=target_resolution_m,
            yRes=target_resolution_m,
            resampleAlg="bilinear",
            creationOptions=["COMPRESS=LZW", "TILED=YES"],
        )
        result_ds = gdal.Warp(out_path, raster_path, options=warp_options)
        if result_ds is None:
            return raster_path
        new_width, new_height = result_ds.RasterXSize, result_ds.RasterYSize
        result_ds = None
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            logger.info(f"Clip remuestreado a {target_resolution_m} m/px: {new_width}x{new_height} px")
            return out_path
    except Exception as e:
        logger.warning(f"No se pudo remuestrear el clip: {e}; se envía el clip original")
    return raster_path


def _ensure_rgb_raster(raster_path, progress_callback=None):
    """
    Si el raster tiene más de 3 bandas, genera un TIFF de solo 3 bandas (RGB) para evitar
    fallos en detección/segmentación. Retorna la ruta del archivo a usar (nuevo o el mismo).
    """
    try:
        import rasterio
    except ImportError:
        return raster_path
    try:
        if progress_callback:
            progress_callback(14, "Comprobando bandas de la imagen...")
            QApplication.processEvents()
        with rasterio.open(raster_path) as src:
            if src.count <= 3:
                return raster_path
            logger.info(f"Raster tiene {src.count} bandas; generando versión RGB (3 bandas) para segmentación")
            out_dir = os.path.dirname(raster_path)
            out_path = os.path.join(out_dir, "clip_rgb.tif")
            profile = src.profile.copy()
            profile.update(count=3, dtype=src.dtypes[0])
            with rasterio.open(out_path, "w", **profile) as dst:
                for i in range(1, 4):
                    band = src.read(i)
                    dst.write(band, i)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
    except Exception as e:
        logger.warning(f"No se pudo generar raster RGB: {e}; se envía el archivo original")
    return raster_path


def _apply_mask_to_multiband(segmented_rgb_path, multiband_clip_path, output_path,
                              mask_path=None, progress_callback=None):
    """
    Aplica la máscara de palmas segmentadas a todas las bandas del clip multibanda
    (multiband_clip_path) y guarda el resultado en output_path.
    Los píxeles de fondo quedan como nodata (0).

    Si mask_path está disponible (TIFF de 1 banda que la API devuelve con la
    máscara binaria real), se usa directamente — más liviano y más correcto que
    inferir la máscara a partir de píxeles no-negros del TIFF RGB (segmented_rgb_path),
    que es el comportamiento de respaldo si mask_path no llega (compatibilidad con
    versiones anteriores de la API que solo devuelven el RGB).

    Usa GDAL (siempre disponible en QGIS). Retorna output_path si tiene éxito, None si falla.
    """
    try:
        from osgeo import gdal
        import numpy as np
    except ImportError as e:
        logger.warning(f"GDAL/numpy no disponible para máscara multibanda: {e}")
        return None

    try:
        if progress_callback:
            progress_callback(0, "Generando capa multibanda segmentada...")

        # Obtener máscara de palmas: preferir el TIFF de máscara real (1 banda, liviano)
        # si la API lo devolvió; si no, inferirla del TIFF RGB (comportamiento anterior).
        if mask_path and os.path.exists(mask_path):
            mask_ds = gdal.Open(mask_path)
            if mask_ds is None:
                raise Exception(f"No se pudo abrir TIFF de máscara: {mask_path}")
            palm_mask = mask_ds.GetRasterBand(1).ReadAsArray() > 0
            seg_w, seg_h = mask_ds.RasterXSize, mask_ds.RasterYSize
            mask_ds = None
            logger.info(f"_apply_mask: usando máscara real de {mask_path}")
        else:
            seg_ds = gdal.Open(segmented_rgb_path)
            if seg_ds is None:
                raise Exception(f"No se pudo abrir TIFF segmentado: {segmented_rgb_path}")

            seg_r = seg_ds.GetRasterBand(1).ReadAsArray()
            seg_g = seg_ds.GetRasterBand(2).ReadAsArray() if seg_ds.RasterCount >= 2 else seg_r
            seg_b = seg_ds.GetRasterBand(3).ReadAsArray() if seg_ds.RasterCount >= 3 else seg_r
            palm_mask = (seg_r > 0) | (seg_g > 0) | (seg_b > 0)
            seg_w, seg_h = seg_ds.RasterXSize, seg_ds.RasterYSize
            seg_ds = None
            logger.info(f"_apply_mask: máscara inferida de TIFF RGB {segmented_rgb_path} (sin mask_path)")

        # Leer clip multibanda
        src_ds = gdal.Open(multiband_clip_path)
        if src_ds is None:
            raise Exception(f"No se pudo abrir clip multibanda: {multiband_clip_path}")

        src_w = src_ds.RasterXSize
        src_h = src_ds.RasterYSize
        src_bands = src_ds.RasterCount
        logger.info(f"_apply_mask: seg={seg_w}x{seg_h}, clip={src_w}x{src_h}, bandas={src_bands}")

        # Ajustar máscara si las dimensiones difieren (resize por vecino más cercano puro numpy)
        if seg_w != src_w or seg_h != src_h:
            logger.info(f"_apply_mask: redimensionando máscara {seg_w}x{seg_h} -> {src_w}x{src_h}")
            y_idx = (np.arange(src_h) * seg_h / src_h).astype(int).clip(0, seg_h - 1)
            x_idx = (np.arange(src_w) * seg_w / src_w).astype(int).clip(0, seg_w - 1)
            palm_mask = palm_mask[y_idx[:, None], x_idx[None, :]]

        # Crear TIFF de salida con GDAL
        data_type = src_ds.GetRasterBand(1).DataType
        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(output_path, src_w, src_h, src_bands, data_type)
        out_ds.SetGeoTransform(src_ds.GetGeoTransform())
        out_ds.SetProjection(src_ds.GetProjection())

        for i in range(1, src_bands + 1):
            band = src_ds.GetRasterBand(i).ReadAsArray().copy()
            band[~palm_mask] = 0
            out_ds.GetRasterBand(i).WriteArray(band)
            out_ds.GetRasterBand(i).SetNoDataValue(0)

        out_ds.FlushCache()
        out_ds = None
        src_ds = None

        logger.info(f"Capa multibanda segmentada guardada: {output_path} ({src_bands} bandas)")
        return output_path

    except Exception as e:
        logger.warning(f"No se pudo generar capa multibanda segmentada: {e}", exc_info=True)
        return None


# Initialize Qt resources from file resources.py
from ..resources import *

# Configurar logging con ruta segura (evita fallar si QGIS arrancó con el
# directorio de trabajo en un lugar sin permisos de escritura, p.ej. System32).
def _cabeceras_autenticacion():
    """Cabeceras para hablar con el backend (ver client_identity)."""
    from ..client_identity import cabeceras_autenticacion
    return cabeceras_autenticacion()


def _setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(plugin_dir, 'client.log')
        try:
            test_file = os.path.join(plugin_dir, '.write_test')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except (PermissionError, OSError):
            user_temp_dir = os.path.join(os.path.expanduser('~'), '.qgis_segmentador_palmas')
            os.makedirs(user_temp_dir, exist_ok=True)
            log_file = os.path.join(user_temp_dir, 'client.log')
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
    except (PermissionError, OSError):
        logging.getLogger(__name__).debug(
            "Fallo no crítico; se continúa.", exc_info=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )

_setup_logging()
logger = logging.getLogger(__name__)

PLUGIN_NAME = 'SegmentadorPalmas'

def get_lot_id_from_feature(feature):
    """
    Obtiene el ID del lote de una feature usando el FID directamente.
    El FID es único, inmutable y no depende de nombres de columnas.
    
    Args:
        feature: QgsFeature del lote seleccionado
    
    Returns:
        str: FID del lote como string
    """
    try:
        # Usar FID directamente - es único e inmutable
        return str(feature.id())
    except Exception:
        # Último recurso: representación genérica
        return str(feature.id())

def _friendly_processing_phase(progress_percent):
    """
    Traduce el progreso numérico (0-100) que reporta la API a una frase clara sobre
    qué está pasando realmente en el servidor. Los rangos siguen las etapas reales
    de process_drone_image en palm_drone_inference.py (recorte -> carga de modelo ->
    SAHI+YOLO -> generación de máscaras -> empaquetado de resultados).
    """
    if progress_percent >= 100:
        return "Segmentación completada. Descargando resultados..."
    if progress_percent >= 90:
        return "Generando resultados de la segmentación..."
    if progress_percent >= 60:
        return "Ejecutando segmentación con Inteligencia Artificial (detectando palmas)..."
    if progress_percent >= 11:
        return "Preparando imagen para el análisis en el servidor..."
    return "Iniciando procesamiento en el servidor..."


class SegmentationCancelled(Exception):
    """Excepción cuando el usuario cancela el proceso desde el diálogo de progreso."""
    pass


class _MultipartStreamReader:
    """
    Construye y transmite un cuerpo multipart/form-data manualmente, sin depender
    de requests_toolbelt (no viene instalado en el Python de QGIS). requests trata
    cualquier objeto con .read() como un stream y lo va leyendo en trozos al enviar
    la petición, lo que permite reportar progreso real de subida con solo `requests`.
    """
    def __init__(self, fields, files_dict, on_progress=None):
        boundary = uuid.uuid4().hex
        self.boundary = boundary
        self.content_type = f'multipart/form-data; boundary={boundary}'
        self._on_progress = on_progress

        file_meta = {
            'orthoimage': ('image.tif', 'image/tiff'),
            'shapefile':  ('lots.shp',  'application/octet-stream'),
            'dbf_file':   ('lots.dbf',  'application/octet-stream'),
            'shx_file':   ('lots.shx',  'application/octet-stream'),
            'prj_file':   ('lots.prj',  'application/octet-stream'),
        }

        # Lista de trozos de bytes en el orden en que se envían; los archivos grandes
        # se referencian directamente (sin copiar) para no duplicar memoria.
        self._chunks = []
        for key, value in fields.items():
            self._chunks.append(
                (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                 f'{value}\r\n').encode('utf-8')
            )
        for key, (fname, ctype) in file_meta.items():
            if key not in files_dict:
                continue
            file_bytes = files_dict[key][1]
            self._chunks.append(
                (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{fname}"\r\n'
                 f'Content-Type: {ctype}\r\n\r\n').encode('utf-8')
            )
            self._chunks.append(file_bytes)
            self._chunks.append(b'\r\n')
        self._chunks.append(f'--{boundary}--\r\n'.encode('utf-8'))

        self._total = sum(len(c) for c in self._chunks)
        self._chunk_idx = 0
        self._pos_in_chunk = 0
        self._read_total = 0

    def __len__(self):
        return self._total

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._total - self._read_total
        result = bytearray()
        remaining = size
        while remaining > 0 and self._chunk_idx < len(self._chunks):
            chunk = self._chunks[self._chunk_idx]
            avail = len(chunk) - self._pos_in_chunk
            take = min(avail, remaining)
            result += chunk[self._pos_in_chunk:self._pos_in_chunk + take]
            self._pos_in_chunk += take
            remaining -= take
            if self._pos_in_chunk >= len(chunk):
                self._chunk_idx += 1
                self._pos_in_chunk = 0
        self._read_total += len(result)
        if self._on_progress:
            self._on_progress(self._read_total, self._total)
        return bytes(result)


class LocalSegmentador:
    def __init__(self, api_url=None, progress_callback=None, info_callback=None, dockwidget=None, cancel_flag=None):
        # URL de la API en producción
        self.api_url = api_url or API_BASE_URL
        self.progress_callback = progress_callback
        self.info_callback = info_callback
        self.dockwidget = dockwidget
        self.cancel_flag = cancel_flag  # dict compartido: si cancel_flag['cancelled'] el hilo debe salir
        self.last_output_shapefile = None
        logger.info(f"Inicializando LocalSegmentador con API: {self.api_url}")
    
    def safe_info_callback(self, message):
        """Actualiza el mensaje informativo (tamaño, peso, etc.) desde el hilo de trabajo."""
        if self.info_callback:
            try:
                QTimer.singleShot(0, lambda m=message: self.info_callback(m))
            except Exception as e:
                logger.error(f"Error en safe_info_callback: {str(e)}")
    
    def safe_progress_callback(self, value, message):
        """
        Ejecuta el callback de progreso de forma segura desde cualquier hilo.
        Usa QTimer para ejecutar en el hilo principal de Qt.
        Captura value y message por valor (v=, m=) para evitar que la lambda
        use valores ya modificados cuando se ejecute.
        """
        if self.progress_callback:
            try:
                # Capturar por valor para que cada timer tenga su propio mensaje
                QTimer.singleShot(0, lambda v=value, m=message: self.progress_callback(v, m))
            except Exception as e:
                logger.error(f"Error en safe_progress_callback: {str(e)}")

    def calculate_lot_area_hectares(self, geometry, crs):
        """
        Calcula el área del lote en hectáreas basado en el sistema de coordenadas
        """
        try:
            area_sq_units = geometry.area()
            logger.info(f"Área del lote en unidades cuadradas: {area_sq_units:.2f}")
            
            crs_description = crs.description().lower()
            
            if 'utm' in crs_description or 'mercator' in crs_description or 'web mercator' in crs_description:
                # CRS en metros, convertir directamente a hectáreas
                area_hectares = area_sq_units / 10000.0  # 1 hectárea = 10,000 m²
                logger.info(f"CRS en metros detectado, área en hectáreas: {area_hectares:.2f}")
                return area_hectares
            elif 'geographic' in crs_description or 'wgs84' in crs_description or 'lat/lon' in crs_description:
                # CRS geográfico, necesitamos convertir a un CRS proyectado para calcular área
                try:
                    # Usar UTM más cercano basado en la ubicación del lote
                    utm_crs = self.get_appropriate_utm_crs(geometry)
                    transform = QgsCoordinateTransform(crs, utm_crs, QgsProject.instance())
                    transformed_geom = geometry
                    transformed_geom.transform(transform)
                    area_sq_meters = transformed_geom.area()
                    area_hectares = area_sq_meters / 10000.0
                    logger.info(f"CRS geográfico detectado, área convertida a hectáreas: {area_hectares:.2f}")
                    return area_hectares
                except Exception as e:
                    logger.warning(f"No se pudo convertir área geográfica: {str(e)}")
                    # Usar un factor de conversión aproximado (esto es menos preciso)
                    area_hectares = area_sq_units * 0.0001  # Factor aproximado
                    logger.info(f"Usando factor de conversión aproximado, área en hectáreas: {area_hectares:.2f}")
                    return area_hectares
            else:
                # CRS desconocido, usar factor de conversión aproximado
                area_hectares = area_sq_units * 0.0001  # Factor aproximado
                logger.info(f"CRS desconocido, usando factor aproximado, área en hectáreas: {area_hectares:.2f}")
                return area_hectares
                
        except Exception as e:
            logger.error(f"Error al calcular área del lote: {str(e)}")
            return None

    def get_appropriate_utm_crs(self, geometry):
        """
        Determina el CRS UTM más apropiado basado en la ubicación de la geometría
        """
        try:
            # Obtener el centro de la geometría
            centroid = geometry.centroid()
            lon = centroid.x()
            lat = centroid.y()
            
            # Determinar la zona UTM basada en la longitud
            utm_zone = int((lon + 180) / 6) + 1
            
            # Determinar el hemisferio basado en la latitud
            if lat >= 0:
                utm_epsg = f"EPSG:326{utm_zone:02d}"  # UTM Norte
            else:
                utm_epsg = f"EPSG:327{utm_zone:02d}"  # UTM Sur
            
            logger.info(f"Centro del lote: {lon:.6f}, {lat:.6f} -> UTM zona {utm_zone} -> {utm_epsg}")
            return QgsCoordinateReferenceSystem(utm_epsg)
        except Exception as e:
            logger.warning(f"Error al determinar UTM apropiado: {str(e)}")
            # Fallback a UTM 18N (Colombia)
            return QgsCoordinateReferenceSystem("EPSG:32618")

    def upload_files_with_progress(self, files, data, progress_callback=None):
        """
        Sube archivos con progreso en tiempo real usando streaming
        """
        try:
            logger.info("Iniciando subida de archivos con progreso")
            
            # Crear un adaptador personalizado para requests que reporte progreso
            class ProgressUploadAdapter:
                def __init__(self, files, data, progress_callback):
                    self.files = files
                    self.data = data
                    self.progress_callback = progress_callback
                    self.uploaded_size = 0
                    self.total_size = self._calculate_total_size()
                    self.start_time = None
                
                def _calculate_total_size(self):
                    """Calcula el tamaño total de todos los archivos"""
                    total = 0
                    for file_data in self.files.values():
                        if isinstance(file_data, tuple) and len(file_data) > 1:
                            total += len(file_data[1])
                    return total
                
                def _progress_callback(self, chunk):
                    """Callback para cada chunk enviado"""
                    if self.start_time is None:
                        self.start_time = time.time()
                    self.uploaded_size += len(chunk)
                    if self.total_size > 0 and self.progress_callback:
                        upload_percent = min(30 + int((self.uploaded_size / self.total_size) * 25), 55)
                        mb_uploaded = self.uploaded_size / (1024*1024)
                        mb_total = self.total_size / (1024*1024)
                        elapsed = time.time() - self.start_time
                        if elapsed > 0:
                            speed_kbps = (self.uploaded_size / elapsed) / 1024
                            speed_str = f"{speed_kbps/1024:.1f} MB/s" if speed_kbps >= 1024 else f"{speed_kbps:.0f} KB/s"
                        else:
                            speed_str = "..."
                        message = f"Subiendo... {mb_uploaded:.1f}/{mb_total:.1f} MB — {speed_str}"
                        self.progress_callback(upload_percent, message)
                        # Forzar procesamiento de eventos
                        QApplication.processEvents()
                        # Pequeña pausa para no bloquear
                        time.sleep(0.01)
                
                def upload(self):
                    """Realiza la subida con progreso"""
                    try:
                        # Configurar la sesión de requests con timeout más largo
                        session = requests.Session()
                        session.timeout = 300  # 5 minutos de timeout
                        
                        # Realizar la petición POST con múltiples intentos
                        max_retries = 3
                        for attempt in range(max_retries):
                            try:
                                logger.info(f"Intento de subida {attempt + 1}/{max_retries}")
                                # Agregar X-API-Key a los headers
                                auth_headers = _cabeceras_autenticacion()
                                logger.info(f"Autenticación X-API-Key configurada para la petición")
                                
                                response = session.post(
                                    SEGMENT_PALMS_ENDPOINT,
                                    files=self.files,
                                    data=self.data,
                                    headers=auth_headers,
                                    stream=True,
                                    timeout=300
                                )
                                
                                # Verificar que la respuesta sea válida
                                if response.status_code in [200, 201]:
                                    logger.info(f"Subida exitosa en intento {attempt + 1}")
                                    return response
                                else:
                                    logger.warning(f"Respuesta del servidor: {response.status_code} - {response.text}")
                                    if attempt < max_retries - 1:
                                        time.sleep(2)  # Esperar antes del siguiente intento
                                        continue
                                    else:
                                        raise Exception(f"Error del servidor: {response.status_code} - {response.text}")
                                        
                            except requests.exceptions.Timeout:
                                logger.warning(f"Timeout en intento {attempt + 1}")
                                if attempt < max_retries - 1:
                                    time.sleep(5)  # Esperar más tiempo antes del siguiente intento
                                    continue
                                else:
                                    raise Exception("Timeout después de múltiples intentos")
                            except requests.exceptions.ConnectionError:
                                logger.warning(f"Error de conexión en intento {attempt + 1}")
                                if attempt < max_retries - 1:
                                    time.sleep(5)
                                    continue
                                else:
                                    raise Exception("Error de conexión después de múltiples intentos")
                        
                        raise Exception("No se pudo completar la subida después de múltiples intentos")
                        
                    except Exception as e:
                        logger.error(f"Error en la subida: {str(e)}")
                        raise
            
            # Crear el adaptador y realizar la subida
            adapter = ProgressUploadAdapter(files, data, progress_callback)
            response = adapter.upload()
            
            return response
            
        except Exception as e:
            logger.error(f"Error en upload_files_with_progress: {str(e)}")
            raise

    def upload_files_with_real_streaming(self, files, data, progress_callback=None):
        """
        Sube archivos usando streaming real para evitar bloqueos
        """
        try:
            logger.info("Iniciando subida con streaming real")
            
            import requests
            from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor
            
            # Crear el encoder multipart
            encoder = MultipartEncoder(
                fields={
                    'slice_size': data['slice_size'],
                    'overlap_ratio': data['overlap_ratio'],
                    'confidence_threshold': data['confidence_threshold'],
                    'orthoimage': ('image.tif', files['orthoimage'][1], 'image/tiff')
                }
            )
            
            # Crear el monitor para el progreso
            _upload_start = [None]
            def progress_monitor(monitor):
                if progress_callback:
                    if _upload_start[0] is None:
                        _upload_start[0] = time.time()
                    total_size = encoder.len
                    uploaded_size = monitor.bytes_read
                    if total_size > 0:
                        upload_percent = min(30 + int((uploaded_size / total_size) * 25), 55)
                        mb_uploaded = uploaded_size / (1024*1024)
                        mb_total = total_size / (1024*1024)
                        elapsed = time.time() - _upload_start[0]
                        if elapsed > 0:
                            speed_kbps = (uploaded_size / elapsed) / 1024
                            speed_str = f"{speed_kbps/1024:.1f} MB/s" if speed_kbps >= 1024 else f"{speed_kbps:.0f} KB/s"
                        else:
                            speed_str = "..."
                        message = f"Subiendo... {mb_uploaded:.1f}/{mb_total:.1f} MB — {speed_str}"
                        progress_callback(upload_percent, message)
                        QApplication.processEvents()
            
            # Crear el monitor
            monitor = MultipartEncoderMonitor(encoder, progress_monitor)
            
            # Configurar headers con X-API-Key
            headers = {
                'X-API-Key': API_KEY,
                'Content-Type': monitor.content_type,
                'Content-Length': str(monitor.len)
            }
            logger.info(f"Autenticación X-API-Key configurada para la petición con streaming")
            
            # Realizar la petición con streaming
            session = requests.Session()
            session.timeout = 300  # 5 minutos
            
            response = session.post(
                SEGMENT_PALMS_ENDPOINT,
                data=monitor,
                headers=headers,
                stream=True,
                timeout=300
            )
            
            return response
            
        except ImportError:
            logger.warning("requests_toolbelt no disponible, usando método estándar")
            # Fallback al método estándar si no está disponible requests_toolbelt
            return self.upload_files_with_progress(files, data, progress_callback)
        except Exception as e:
            logger.error(f"Error en upload_files_with_real_streaming: {str(e)}")
            # Fallback al método estándar
            return self.upload_files_with_progress(files, data, progress_callback)

    def clip_raster_with_shapefile(self, raster_path, shapefile_path, progress_callback=None):
        """
        Recorta el raster usando el shapefile como máscara.
        Retorna la ruta del raster recortado.
        """
        try:
            logger.info(f"Iniciando clip del raster: {raster_path}")
            logger.info(f"Usando shapefile como máscara: {shapefile_path}")
            
            if progress_callback:
                progress_callback(4, "Preparando clip del raster...")
                QApplication.processEvents()
            
            # Crear directorio temporal para el raster recortado
            temp_dir = tempfile.mkdtemp(prefix="raster_clip_")
            clipped_raster_path = os.path.join(temp_dir, "clipped_image.tif")
            
            # Cargar las capas
            raster_layer = QgsRasterLayer(raster_path, "raster")
            vector_layer = QgsVectorLayer(shapefile_path, "vector", "ogr")
            
            if not raster_layer.isValid():
                raise Exception(f"No se pudo cargar el raster: {raster_path}")
            if not vector_layer.isValid():
                raise Exception(f"No se pudo cargar el shapefile: {shapefile_path}")
            
            logger.info("Capas cargadas correctamente")
            
            if progress_callback:
                progress_callback(7, "Ejecutando clip del raster...")
                QApplication.processEvents()
            
            # Inicializar el framework de procesamiento
            try:
                from qgis.core import QgsProcessing
                
                # Verificar si ya hay proveedores registrados
                registry = QgsApplication.processingRegistry()
                existing_providers = registry.providers()
                
                if not existing_providers:
                    # Intentar agregar el proveedor nativo
                    try:
                        from qgis.analysis import QgsNativeAlgorithms
                        native_algorithms = QgsNativeAlgorithms()
                        registry.addProvider(native_algorithms)
                        logger.info("Proveedor de algoritmos nativos registrado")
                    except ImportError:
                        try:
                            from qgis.core import QgsNativeAlgorithms
                            native_algorithms = QgsNativeAlgorithms()
                            registry.addProvider(native_algorithms)
                            logger.info("Proveedor de algoritmos nativos registrado")
                        except ImportError:
                            logger.warning("No se pudo importar QgsNativeAlgorithms, usando proveedores existentes")
                else:
                    logger.info(f"Proveedores de algoritmos ya registrados: {len(existing_providers)}")
                    
            except Exception as e:
                logger.error(f"Error al inicializar algoritmos de procesamiento: {str(e)}")
                raise Exception(f"No se pudieron inicializar los algoritmos de procesamiento: {str(e)}")
            
            # El feedback no se usa en esta implementación simplificada
            
            # Configurar el algoritmo de clip
            alg_name = 'gdal:cliprasterbymasklayer'
            
            # Verificar que el algoritmo esté disponible
            registry = QgsApplication.processingRegistry()
            algorithm = registry.algorithmById(alg_name)
            if not algorithm:
                # Intentar con nombre alternativo
                alg_name = 'native:cliprasterbymasklayer'
                algorithm = registry.algorithmById(alg_name)
                if not algorithm:
                    raise Exception(f"Algoritmo de clip no disponible. Algoritmos probados: gdal:cliprasterbymasklayer, native:cliprasterbymasklayer")
            
            logger.info(f"Algoritmo encontrado: {alg_name}")
            
            # Parámetros para el algoritmo
            params = {
                'INPUT': raster_path,
                'MASK': shapefile_path,
                'SOURCE_CRS': None,  # Usar CRS del raster
                'TARGET_CRS': None,  # Mantener CRS original
                'NODATA': None,      # No especificar nodata
                'ALPHA_BAND': False, # No usar banda alfa
                'CROP_TO_CUTLINE': True,  # Recortar al límite del shapefile
                'KEEP_RESOLUTION': True,  # Mantener resolución original
                'SET_RESOLUTION': False,  # No cambiar resolución
                'X_RESOLUTION': None,
                'Y_RESOLUTION': None,
                'MULTITHREADING': False,  # No usar multithreading para evitar problemas
                'OPTIONS': '',
                'DATA_TYPE': 0,  # Tipo de datos automático
                'EXTRA': '',
                'OUTPUT': clipped_raster_path
            }
            
            logger.info("Ejecutando algoritmo de clip...")
            
            # Ejecutar el algoritmo usando diferentes métodos según la versión de QGIS
            try:
                result = None
                
                # Método 1: Intentar con QgsProcessingUtils (versiones más nuevas)
                if hasattr(QgsProcessingUtils, 'runAlgorithm'):
                    try:
                        result = QgsProcessingUtils.runAlgorithm(alg_name, params)
                        logger.info("Algoritmo ejecutado con QgsProcessingUtils")
                    except Exception as e:
                        logger.warning(f"QgsProcessingUtils falló: {str(e)}")
                
                # Método 2: Intentar con QgsProcessing (versiones intermedias)
                if not result:
                    try:
                        from qgis.core import QgsProcessing
                        if hasattr(QgsProcessing, 'runAlgorithm'):
                            result = QgsProcessing.runAlgorithm(alg_name, params)
                            logger.info("Algoritmo ejecutado con QgsProcessing")
                    except Exception as e:
                        logger.warning(f"QgsProcessing falló: {str(e)}")
                
                # Método 3: Usar el algoritmo directamente (versiones más antiguas)
                if not result:
                    try:
                        algorithm = registry.algorithmById(alg_name)
                        if algorithm:
                            # Crear contexto y feedback para versiones antiguas
                            context = QgsProcessingContext()
                            feedback = QgsProcessingFeedback()
                            result = algorithm.run(params, context, feedback)
                            logger.info("Algoritmo ejecutado directamente")
                    except Exception as e:
                        logger.warning(f"Ejecución directa falló: {str(e)}")
                
                if not result:
                    raise Exception(f"No se pudo ejecutar el algoritmo {alg_name} con ningún método disponible")
                            
            except Exception as e:
                logger.error(f"Error al ejecutar algoritmo de clip: {str(e)}")
                raise Exception(f"Error al ejecutar algoritmo de clip: {str(e)}")
            
            # Extraer la ruta del resultado de manera simple y directa
            logger.info(f"Tipo de resultado: {type(result)}")
            logger.info(f"Contenido del resultado: {result}")
            
            clipped_path = None
            
            # Extraer la ruta de manera directa y simple
            logger.info(f"Extrayendo ruta del resultado: {result}")
            logger.info(f"Tipo del resultado: {type(result)}")
            
            clipped_path = None
            
            # Extraer la ruta de manera simple y directa
            logger.info(f"Resultado completo: {result}")
            logger.info(f"Tipo del resultado: {type(result)}")
            
            # Función auxiliar para buscar rutas de manera recursiva
            def find_raster_path(obj, depth=0):
                if depth > 5:  # Evitar recursión infinita
                    return None
                
                logger.info(f"Buscando ruta en nivel {depth}: {type(obj)}")
                
                if isinstance(obj, str) and (obj.endswith('.tif') or obj.endswith('.tiff')):
                    logger.info(f"Ruta encontrada en nivel {depth}: {obj}")
                    return obj
                elif isinstance(obj, dict):
                    # Buscar en todas las claves del diccionario
                    for key, value in obj.items():
                        logger.info(f"Revisando clave '{key}' en nivel {depth}")
                        path = find_raster_path(value, depth + 1)
                        if path:
                            return path
                elif isinstance(obj, (list, tuple)):
                    # Buscar en todos los elementos de la lista/tupla
                    for i, item in enumerate(obj):
                        logger.info(f"Revisando elemento {i} en nivel {depth}")
                        path = find_raster_path(item, depth + 1)
                        if path:
                            return path
                return None
            
            # Buscar la ruta de manera recursiva
            clipped_path = find_raster_path(result)
            
            if not clipped_path:
                raise Exception(f"No se encontró una ruta válida en el resultado: {result}")
            
            # Verificar que sea una cadena válida
            if not isinstance(clipped_path, str):
                raise Exception(f"La ruta no es una cadena válida: {type(clipped_path)}")
            
            logger.info(f"Ruta final: {clipped_path}")
            
            if not os.path.exists(clipped_path):
                raise Exception(f"El archivo de salida no existe: {clipped_path}")
            
            logger.info(f"Archivo de salida verificado: {clipped_path}")
            
            if progress_callback:
                progress_callback(9, "Clip completado, preparando imagen...")
                QApplication.processEvents()
            
            return clipped_path
                
        except Exception as e:
            logger.error(f"Error en el clip del raster: {str(e)}")
            # Intentar método alternativo usando GDAL directamente
            try:
                logger.info("Intentando método alternativo de clip usando GDAL...")
                return self.clip_raster_gdal_fallback(raster_path, shapefile_path, progress_callback)
            except Exception as fallback_error:
                logger.error(f"Error en método alternativo: {str(fallback_error)}")
                raise Exception(f"Error en el clip del raster: {str(e)}. Método alternativo también falló: {str(fallback_error)}")

    def create_temp_lote_shapefile(self, shapefile_path, lot_id, progress_callback=None):
        """
        Crea un shapefile temporal con solo el lote seleccionado.
        """
        try:
            logger.info(f"Creando shapefile temporal para lote {lot_id}")
            
            if progress_callback:
                progress_callback(3, "Extrayendo lote seleccionado...")
                QApplication.processEvents()
            
            # Crear directorio temporal para el shapefile del lote
            temp_dir = tempfile.mkdtemp(prefix="lote_shapefile_")
            temp_shapefile_path = os.path.join(temp_dir, f"lote_{lot_id}.shp")
            
            # Cargar el shapefile completo
            vector_layer = QgsVectorLayer(shapefile_path, "vector", "ogr")
            if not vector_layer.isValid():
                raise Exception(f"No se pudo cargar el shapefile: {shapefile_path}")
            
            logger.info(f"Shapefile cargado. Total de lotes: {vector_layer.featureCount()}")
            
            # Buscar el lote específico por FID (igual que el Detector)
            lot_found = False
            total_features = vector_layer.featureCount()
            logger.info(f"Total de lotes en el shapefile: {total_features}")
            logger.info(f"Buscando lote con FID: {lot_id}")
            
            # Listar todos los FIDs disponibles para debugging
            available_fids = []
            for feature in vector_layer.getFeatures():
                available_fids.append(str(feature.id()))
            logger.info(f"FIDs disponibles en el shapefile: {available_fids}")
            
            for feature in vector_layer.getFeatures():
                # Usar FID directamente - es único e inmutable
                current_fid = str(feature.id())
                logger.info(f"Revisando lote FID: {current_fid} (buscando: {lot_id})")
                
                if current_fid == str(lot_id):
                    logger.info(f"Lote {lot_id} encontrado")
                    lot_found = True
                    from qgis.core import QgsFeature, QgsVectorFileWriter
                    # Crear una capa temporal con la misma estructura
                    temp_layer = QgsVectorLayer(f"Polygon?crs={vector_layer.crs().authid()}", "temp", "memory")
                    temp_layer.dataProvider().addAttributes(vector_layer.fields())
                    temp_layer.updateFields()
                    # Asegurarse de que la geometría sea simple (no multipolígono)
                    geom = feature.geometry()
                    if geom.isMultipart():
                        simple_geoms = geom.asGeometryCollection()
                        # Tomar solo la primera geometría simple (la más grande)
                        if simple_geoms:
                            simple_geom = max(simple_geoms, key=lambda g: g.area() if hasattr(g, 'area') else 0)
                        else:
                            simple_geom = geom
                    else:
                        simple_geom = geom
                    temp_feature = QgsFeature(temp_layer.fields())
                    temp_feature.setGeometry(simple_geom)
                    for field in vector_layer.fields():
                        temp_feature[field.name()] = feature[field.name()]
                    temp_layer.dataProvider().addFeatures([temp_feature])
                    
                    # Área del lote en hectáreas, solo informativa: no se usa para
                    # bloquear lotes por tamaño, los grandes se dividen en bloques.
                    area_hectares = self.calculate_lot_area_hectares(simple_geom, vector_layer.crs())
                    if area_hectares is not None:
                        logger.info(f"Área del lote: {area_hectares:.2f} hectáreas")

                    # Guardar como shapefile temporal
                    QgsVectorFileWriter.writeAsVectorFormat(
                        temp_layer,
                        temp_shapefile_path,
                        "utf-8",
                        vector_layer.crs(),
                        "ESRI Shapefile"
                    )
                    logger.info(f"Shapefile temporal creado: {temp_shapefile_path}")
                    break
            
            if not lot_found:
                raise Exception(f"No se encontró el lote {lot_id} en el shapefile")
            
            if progress_callback:
                progress_callback(7, "Recortando imagen (método alternativo)...")
                QApplication.processEvents()
            
            # Retornar tanto la ruta del shapefile como el área calculada
            return temp_shapefile_path, area_hectares if 'area_hectares' in locals() else None
            
        except Exception as e:
            logger.error(f"Error al crear shapefile temporal del lote: {str(e)}")
            raise Exception(f"Error al crear shapefile temporal del lote: {str(e)}")

    def clip_raster_gdal_fallback(self, raster_path, shapefile_path, progress_callback=None):
        """
        Método alternativo de clip usando GDAL directamente.
        """
        try:
            logger.info("Usando método alternativo de clip con GDAL")
            
            if progress_callback:
                progress_callback(5, "Usando método alternativo de clip del raster...")
                QApplication.processEvents()
            
            # Crear directorio temporal
            temp_dir = tempfile.mkdtemp(prefix="raster_clip_gdal_")
            clipped_raster_path = os.path.join(temp_dir, "clipped_image.tif")
            
            # Usar QgsRasterLayer para obtener información del raster
            raster_layer = QgsRasterLayer(raster_path, "raster")
            if not raster_layer.isValid():
                raise Exception(f"No se pudo cargar el raster: {raster_path}")
            
            # Obtener la extensión del shapefile
            vector_layer = QgsVectorLayer(shapefile_path, "vector", "ogr")
            if not vector_layer.isValid():
                raise Exception(f"No se pudo cargar el shapefile: {shapefile_path}")
            
            # Obtener la extensión del shapefile
            extent = vector_layer.extent()
            xmin, ymin, xmax, ymax = extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()
            
            logger.info(f"Extensión del shapefile: {xmin}, {ymin}, {xmax}, {ymax}")
            
            # Usar el algoritmo de recorte por extensión que es más simple
            alg_name = 'gdal:cliprasterbyextent'
            
            # Verificar si está disponible
            registry = QgsApplication.processingRegistry()
            algorithm = registry.algorithmById(alg_name)
            if not algorithm:
                raise Exception(f"Algoritmo de recorte por extensión no disponible: {alg_name}")
            
            # Parámetros para recorte por extensión
            params = {
                'INPUT': raster_path,
                'PROJWIN': f"{xmin},{xmax},{ymin},{ymax}",
                'NODATA': None,
                'OPTIONS': '',
                'DATA_TYPE': 0,
                'EXTRA': '',
                'OUTPUT': clipped_raster_path
            }
            
            # Ejecutar el algoritmo usando diferentes métodos según la versión de QGIS
            result = None
            
            # Método 1: Intentar con QgsProcessingUtils (versiones más nuevas)
            if hasattr(QgsProcessingUtils, 'runAlgorithm'):
                try:
                    result = QgsProcessingUtils.runAlgorithm(alg_name, params)
                    logger.info("Algoritmo alternativo ejecutado con QgsProcessingUtils")
                except Exception as e:
                    logger.warning(f"QgsProcessingUtils alternativo falló: {str(e)}")
            
            # Método 2: Intentar con QgsProcessing (versiones intermedias)
            if not result:
                try:
                    from qgis.core import QgsProcessing
                    if hasattr(QgsProcessing, 'runAlgorithm'):
                        result = QgsProcessing.runAlgorithm(alg_name, params)
                        logger.info("Algoritmo alternativo ejecutado con QgsProcessing")
                except Exception as e:
                    logger.warning(f"QgsProcessing alternativo falló: {str(e)}")
            
            # Método 3: Usar el algoritmo directamente (versiones más antiguas)
            if not result:
                try:
                    algorithm = registry.algorithmById(alg_name)
                    if algorithm:
                        # Crear contexto y feedback para versiones antiguas
                        context = QgsProcessingContext()
                        feedback = QgsProcessingFeedback()
                        result = algorithm.run(params, context, feedback)
                        logger.info("Algoritmo alternativo ejecutado directamente")
                except Exception as e:
                    logger.warning(f"Ejecución directa alternativa falló: {str(e)}")
            
            if not result:
                raise Exception(f"No se pudo ejecutar el algoritmo alternativo {alg_name} con ningún método disponible")
            
            # Extraer la ruta del resultado de manera simple y directa
            logger.info(f"Tipo de resultado alternativo: {type(result)}")
            logger.info(f"Contenido del resultado alternativo: {result}")
            
            clipped_path = None
            
            # Extraer la ruta de manera directa y simple
            logger.info(f"Extrayendo ruta del resultado alternativo: {result}")
            logger.info(f"Tipo del resultado alternativo: {type(result)}")
            
            clipped_path = None
            
            # Extraer la ruta de manera simple y directa
            logger.info(f"Resultado alternativo completo: {result}")
            logger.info(f"Tipo del resultado alternativo: {type(result)}")
            
            # Función auxiliar para buscar rutas de manera recursiva
            def find_raster_path_alt(obj, depth=0):
                if depth > 5:  # Evitar recursión infinita
                    return None
                
                logger.info(f"Buscando ruta alternativo en nivel {depth}: {type(obj)}")
                
                if isinstance(obj, str) and (obj.endswith('.tif') or obj.endswith('.tiff')):
                    logger.info(f"Ruta encontrada alternativo en nivel {depth}: {obj}")
                    return obj
                elif isinstance(obj, dict):
                    # Buscar en todas las claves del diccionario
                    for key, value in obj.items():
                        logger.info(f"Revisando clave '{key}' alternativo en nivel {depth}")
                        path = find_raster_path_alt(value, depth + 1)
                        if path:
                            return path
                elif isinstance(obj, (list, tuple)):
                    # Buscar en todos los elementos de la lista/tupla
                    for i, item in enumerate(obj):
                        logger.info(f"Revisando elemento {i} alternativo en nivel {depth}")
                        path = find_raster_path_alt(item, depth + 1)
                        if path:
                            return path
                return None
            
            # Buscar la ruta de manera recursiva
            clipped_path = find_raster_path_alt(result)
            
            if not clipped_path:
                raise Exception(f"No se encontró una ruta válida en el resultado alternativo: {result}")
            
            # Verificar que sea una cadena válida
            if not isinstance(clipped_path, str):
                raise Exception(f"La ruta alternativo no es una cadena válida: {type(clipped_path)}")
            
            logger.info(f"Ruta final alternativo: {clipped_path}")
            
            if not os.path.exists(clipped_path):
                raise Exception(f"El archivo de salida alternativo no existe: {clipped_path}")
            
            logger.info(f"Archivo de salida alternativo verificado: {clipped_path}")
            
            if progress_callback:
                progress_callback(9, "Clip del raster completado (método alternativo)")
                QApplication.processEvents()
            
            return clipped_path
                
        except Exception as e:
            logger.error(f"Error en método alternativo de clip: {str(e)}")
            raise Exception(f"Error en método alternativo de clip: {str(e)}")

    def process_segmentation(self, image_path, lotes_path, lot_id, slice_height=None, slice_width=None, overlap_ratio=None, confidence_threshold=None, standard_resolution=None, res_threshold=None, output_folder=None):
        """
        Procesa la segmentación usando la API del servidor.
        Parámetros alineados con config/model_config.yaml (imgsz 1024, etc.); la API los usa en inferencia.
        """
        # Usar valores por defecto de config.py si no se proporcionan
        if slice_height is None:
            slice_height = DEFAULT_SLICE_HEIGHT
        if slice_width is None:
            slice_width = DEFAULT_SLICE_WIDTH
        if overlap_ratio is None:
            overlap_ratio = DEFAULT_OVERLAP_RATIO
        if confidence_threshold is None:
            confidence_threshold = DEFAULT_CONFIDENCE_THRESHOLD
        if standard_resolution is None:
            standard_resolution = DEFAULT_STANDARD_RESOLUTION
        if res_threshold is None:
            res_threshold = DEFAULT_RES_THRESHOLD
        
        temp_dir = None
        output_shapefile = None
        
        try:
            logger.info(f"Iniciando nueva segmentación")
            logger.info(f"Imagen: {image_path}")
            logger.info(f"Shapefile: {lotes_path}")
            logger.info(f"FID del lote: {lot_id}")
            
            # Limpiar resultados anteriores si existen
            if self.last_output_shapefile and os.path.exists(self.last_output_shapefile):
                logger.info(f"Limpiando resultados anteriores: {self.last_output_shapefile}")
                try:
                    base_path = os.path.splitext(self.last_output_shapefile)[0]
                    files_cleaned = 0
                    for ext in ['.tif', '.tiff']:
                        old_file = base_path + ext
                        if os.path.exists(old_file):
                            try:
                                os.remove(old_file)
                                files_cleaned += 1
                                logger.info(f"Archivo eliminado: {old_file}")
                            except Exception as e:
                                logger.warning(f"No se pudo eliminar {old_file}: {str(e)}")
                    
                    if files_cleaned > 0:
                        logger.info(f"{files_cleaned} archivos anteriores eliminados")
                        
                except Exception as e:
                    logger.error(f"Error al limpiar archivos anteriores: {str(e)}")

            # El archivo de salida será determinado por el contenido del ZIP que envía la API
            output_shapefile = None
            logger.info("El archivo de salida será determinado por el contenido del ZIP de la API")
            
            # Normalizar lot_id (FID) como string
            try:
                if isinstance(lot_id, (int, float)):
                    lot_id_normalized = str(int(lot_id)) if float(lot_id).is_integer() else str(lot_id)
                else:
                    lot_id_normalized = str(lot_id).strip()
                
                # Validar que el FID sea un número válido
                try:
                    fid_int = int(lot_id_normalized)
                    if fid_int < 0:
                        raise ValueError("FID no puede ser negativo")
                except ValueError as ve:
                    raise ValueError(f"FID inválido: {lot_id_normalized}. Debe ser un número entero no negativo. Error: {str(ve)}")
                    
            except Exception as e:
                logger.error(f"Error al normalizar FID: {str(e)}")
                raise ValueError(f"Error al procesar FID: {str(e)}")
            
            logger.info(f"FID normalizado: {lot_id_normalized}")
            
            # Verificar que todos los archivos del shapefile existan
            base_shapefile_path = os.path.splitext(lotes_path)[0]
            shapefile_files = {
                'shp': lotes_path,
                'dbf': base_shapefile_path + '.dbf',
                'shx': base_shapefile_path + '.shx',
                'prj': base_shapefile_path + '.prj'
            }
            
            missing_files = []
            for key, file_path in shapefile_files.items():
                if not os.path.exists(file_path):
                    missing_files.append(f"{key.upper()}: {file_path}")
            
            if missing_files:
                raise Exception(f"Faltan archivos necesarios del shapefile:\n" + "\n".join(missing_files))
            
            logger.info("Todos los archivos del shapefile encontrados")
            
            # Crear shapefile temporal con solo el lote seleccionado
            temp_lote_shapefile = None
            clipped_raster_path = None
            multiband_clip_for_indices = None
            try:
                logger.info(f"Creando shapefile temporal para lote {lot_id_normalized}")
                temp_lote_shapefile, lot_area_hectares = self.create_temp_lote_shapefile(
                    lotes_path, lot_id_normalized, self.progress_callback
                )
                
                if lot_area_hectares is not None:
                    if self.progress_callback:
                        self.progress_callback(2, f"Lote {lot_id_normalized} preparado - Área: {lot_area_hectares:.2f} hectáreas")
                        QApplication.processEvents()
                
                # Hacer clip del raster usando solo el lote seleccionado.
                clip_size_mb_before_resample = None  # Peso del clip (para mensaje si luego se remuestrea)
                # El clip (GDAL) solo recorta: no cambia el tamaño de píxel del TIFF.
                # El valor "Tamaño de píxel" que ves en QGIS se mantiene igual antes y después del clip.
                logger.info(f"Realizando clip del raster para reducir tamaño")
                logger.info(f"Archivo original: {image_path}")
                logger.info(f"Tamaño del archivo original: {os.path.getsize(image_path) / (1024*1024):.2f} MB")
                logger.info(f"Shapefile del lote seleccionado: {temp_lote_shapefile}")
                
                if self.progress_callback:
                    self.progress_callback(3, "Por favor espere, esta operación puede tardar algunos minutos...")
                    QApplication.processEvents()
                
                if self.progress_callback:
                    self.progress_callback(4, "Recortando imagen al área del lote...")
                
                clipped_raster_path = self.clip_raster_with_shapefile(image_path, temp_lote_shapefile, self.progress_callback)
                
                logger.info(f"Clip completado. Archivo recortado: {clipped_raster_path}")
                clip_size_mb_before_resample = None
                if os.path.exists(clipped_raster_path):
                    clip_size_mb_before_resample = os.path.getsize(clipped_raster_path) / (1024 * 1024)
                    logger.info(f"Tamaño del archivo recortado: {clip_size_mb_before_resample:.2f} MB")
                else:
                    logger.error(f"El archivo recortado no existe: {clipped_raster_path}")
                    raise Exception("El archivo recortado no se generó correctamente")
                
                # Remuestreo solo en plugin (p. ej. a 10 cm/px): reduce tamaño y acelera la API
                if RESAMPLE_CLIP_TARGET_M_PX is not None and RESAMPLE_CLIP_TARGET_M_PX > 0:
                    clipped_raster_path = _resample_clip_to_resolution(
                        clipped_raster_path, RESAMPLE_CLIP_TARGET_M_PX, self.progress_callback
                    )
                    if os.path.exists(clipped_raster_path):
                        logger.info(f"Tamaño tras remuestreo: {os.path.getsize(clipped_raster_path) / (1024*1024):.2f} MB")
                
            except Exception as e:
                logger.error(f"Error al crear shapefile temporal o hacer clip: {str(e)}")
                raise Exception(f"Error al preparar archivos: {str(e)}")
            
            # Guardar ruta del clip multibanda antes de convertir a RGB para índices espectrales
            multiband_clip_for_indices = clipped_raster_path

            # Asegurar 3 bandas (RGB): ortomosaicos con 4+ bandas pueden fallar en segmentación
            clipped_raster_path = _ensure_rgb_raster(clipped_raster_path, self.progress_callback)
            
            # Preparar los archivos para enviar (usar el raster recortado y el shapefile del lote)
            logger.info(f"Preparando archivos para enviar")
            logger.info(f"Archivo raster a enviar: {clipped_raster_path}")
            clipped_size_bytes = os.path.getsize(clipped_raster_path)
            clipped_size_mb = clipped_size_bytes / (1024 * 1024)
            # Mensaje estático: dimensiones y peso del archivo que se envía (tras remuestreo a 10 cm/px si aplica)
            clip_w, clip_h = _get_raster_dimensions(clipped_raster_path)
            if RESAMPLE_CLIP_TARGET_M_PX is not None and RESAMPLE_CLIP_TARGET_M_PX > 0 and clip_size_mb_before_resample is not None:
                peso_texto = f"Peso del clip: {clip_size_mb_before_resample:.1f} MB → tras remuestreo a {RESAMPLE_CLIP_TARGET_M_PX * 100:.0f} cm/px: {clipped_size_mb:.1f} MB"
            else:
                peso_texto = f"Peso (archivo a enviar): {clipped_size_mb:.1f} MB"
                if RESAMPLE_CLIP_TARGET_M_PX is not None and RESAMPLE_CLIP_TARGET_M_PX > 0:
                    peso_texto += f" (remuestreada a {RESAMPLE_CLIP_TARGET_M_PX * 100:.0f} cm/px)"
            if clip_w and clip_h:
                info_line = (
                    f"Imagen a enviar: {clip_w}×{clip_h} px\n"
                    f"{peso_texto}\n"
                    "Por favor espere. Esta operación puede tardar varios minutos."
                )
            else:
                info_line = (
                    f"{peso_texto}\n"
                    "Por favor espere. Esta operación puede tardar varios minutos."
                )
            self.safe_info_callback(info_line)
            self.safe_progress_callback(15, "Preparando envío a la cola de procesamiento...")
            
            # Procesar eventos antes de leer archivos grandes (puede ser bloqueante)
            QApplication.processEvents()
            QApplication.processEvents()
            
            # Enviar los parámetros de segmentación a la API (replican config de entrenamiento + resolución)
            from ..client_identity import get_client_id
            data = {
                'lot_id': lot_id_normalized,
                'slice_height': str(slice_height),
                'slice_width': str(slice_width),
                'overlap_ratio': str(overlap_ratio),
                'confidence_threshold': str(confidence_threshold),
                'standard_resolution': str(standard_resolution),
                'res_threshold': str(res_threshold),
                # El plugin ya tiene su propio clip local (multiband_clip_for_indices) y
                # aplica la máscara ahí mismo (_apply_mask_to_multiband) — no necesita que
                # la API devuelva también el TIFF RGB pesado. Reduce mucho el peso del ZIP.
                'include_rgb_output': 'false',
                'lot_area': str(lot_area_hectares) if lot_area_hectares is not None else '',
                'client_id': get_client_id(),
            }
            logger.info(f"Parámetros enviados a la API: slice={slice_height}x{slice_width}, overlap={overlap_ratio}, conf={confidence_threshold}, res_std={standard_resolution}, res_thr={res_threshold}, include_rgb_output=false")
            
            # Iniciar la segmentación en un hilo separado usando cola async
            def segmentation_thread():
                nonlocal temp_dir, output_shapefile, temp_lote_shapefile, clipped_raster_path, multiband_clip_for_indices
                job_id = None
                try:
                    if self.cancel_flag and self.cancel_flag.get("cancelled"):
                        logger.info("[THREAD] Cancelado por el usuario antes de iniciar")
                        segmentation_result["cancelled"] = True
                        segmentation_result["completed"] = True
                        return
                    logger.info(f"[THREAD] Hilo de segmentación iniciado")
                    logger.info(f"[THREAD] Enviando petición al servidor (modo cola async)")
                    
                    # Leer el raster recortado y los archivos del shapefile temporal
                    files = {}
                    try:
                        # Leer imagen recortada (mucho más pequeña)
                        logger.info(f"Leyendo imagen recortada: {clipped_raster_path}")
                        with open(clipped_raster_path, 'rb') as img_file:
                            files['orthoimage'] = ('image.tif', img_file.read())
                        
                        # Leer archivos shapefile temporal (solo el lote seleccionado)
                        base_temp_shapefile = os.path.splitext(temp_lote_shapefile)[0]
                        with open(temp_lote_shapefile, 'rb') as shp_file:
                            files['shapefile'] = ('lots.shp', shp_file.read())
                        with open(base_temp_shapefile + '.dbf', 'rb') as dbf_file:
                            files['dbf_file'] = ('lots.dbf', dbf_file.read())
                        with open(base_temp_shapefile + '.shx', 'rb') as shx_file:
                            files['shx_file'] = ('lots.shx', shx_file.read())
                        with open(base_temp_shapefile + '.prj', 'rb') as prj_file:
                            files['prj_file'] = ('lots.prj', prj_file.read())
                        
                        logger.info("Todos los archivos leídos correctamente")
                        
                    except Exception as e:
                        logger.error(f"Error al leer archivos: {str(e)}")
                        raise Exception(f"Error al leer archivos para subida: {str(e)}")
                    
                    if self.cancel_flag and self.cancel_flag.get("cancelled"):
                        logger.info("[THREAD] Cancelado por el usuario tras leer archivos")
                        segmentation_result["cancelled"] = True
                        segmentation_result["completed"] = True
                        return
                    # Calcular tamaño total de archivos
                    total_size = 0
                    for file_data in files.values():
                        if isinstance(file_data, tuple) and len(file_data) > 1:
                            total_size += len(file_data[1])
                    
                    total_size_mb = total_size / (1024 * 1024)
                    logger.info(f"Tamaño total de archivos a subir: {total_size_mb:.2f} MB")
                    
                    # Enviar a la cola async
                    auth_headers = _cabeceras_autenticacion()
                    
                    # Mensaje inicial: tamaño del archivo antes de iniciar subida
                    if self.progress_callback:
                        _set_progress(15, f"Iniciando subida ({total_size_mb:.1f} MB)...")

                    if self.cancel_flag and self.cancel_flag.get("cancelled"):
                        logger.info("[THREAD] Cancelado por el usuario antes de enviar")
                        segmentation_result["cancelled"] = True
                        segmentation_result["completed"] = True
                        return

                    logger.info(f"[THREAD] Enviando POST a {SEGMENT_PALMS_ASYNC_ENDPOINT}")
                    logger.info(f"[THREAD] Tamaño de archivos: {total_size_mb:.2f} MB")

                    try:
                        logger.info("[THREAD] Iniciando requests.post()...")
                        post_start = time.time()

                        try:
                            def _on_upload_progress(uploaded, total, _start=[None]):
                                if _start[0] is None:
                                    _start[0] = time.time()
                                elapsed = time.time() - _start[0]
                                mb_up = uploaded / (1024 * 1024)
                                mb_tot = total / (1024 * 1024)
                                if elapsed > 0.3:
                                    speed_kbps = (uploaded / elapsed) / 1024
                                    speed_str = f"{speed_kbps/1024:.1f} MB/s" if speed_kbps >= 1024 else f"{speed_kbps:.0f} KB/s"
                                else:
                                    speed_str = "..."
                                # Reemplaza la última línea de info_line ("Por favor espere...") con la velocidad en vivo
                                info_parts = info_line.rsplit('\n', 1)
                                base_info = info_parts[0] if len(info_parts) > 1 else info_line
                                _set_info(f"{base_info}\nSubiendo... {mb_up:.1f}/{mb_tot:.1f} MB — {speed_str}")

                                # Barra de progreso: 15-45% proporcional a los bytes realmente subidos
                                # (la velocidad y el detalle de MB ya se muestran arriba, en el recuadro de info)
                                upload_fraction = (uploaded / total) if total > 0 else 0
                                bar_value = 15 + int(upload_fraction * 30)
                                _set_progress(bar_value, "Enviando lote al servidor...")

                            reader = _MultipartStreamReader(data, files, on_progress=_on_upload_progress)
                            upload_headers = {**auth_headers, 'Content-Type': reader.content_type}
                            response = requests.post(
                                SEGMENT_PALMS_ASYNC_ENDPOINT,
                                data=reader,
                                headers=upload_headers,
                                timeout=UPLOAD_TIMEOUT
                            )
                        except Exception as e:
                            logger.warning(f"[THREAD] Fallo el streaming con progreso ({str(e)}), subiendo sin indicador de velocidad")
                            response = requests.post(
                                SEGMENT_PALMS_ASYNC_ENDPOINT,
                                files=files,
                                data=data,
                                headers=auth_headers,
                                timeout=UPLOAD_TIMEOUT
                            )

                        post_time = time.time() - post_start
                        logger.info(f"[THREAD] requests.post() completado en {post_time:.2f}s. Status: {response.status_code}")
                        
                        # Actualizar progreso después de subir
                        if self.progress_callback:
                            _set_progress(45, f"Solicitud enviada a la cola. Esperando procesamiento en servidor...")
                            QApplication.processEvents()
                    except Exception as e:
                        logger.error(f"[THREAD] Error en requests.post: {str(e)}")
                        logger.error(f"[THREAD] Traceback: {traceback.format_exc()}")
                        raise
                    
                    if response.status_code != 200:
                        error_content = response.text
                        if response.status_code == 404:
                            error_msg = f"Error del servidor: 404 - El endpoint no fue encontrado. Verifica que la API esté funcionando correctamente."
                        elif response.status_code == 402:
                            try:
                                detalle = response.json().get("detail", error_content)
                            except Exception:
                                detalle = error_content
                            error_msg = f"Créditos de procesamiento insuficientes.\n\n{detalle}\n\nCompra un paquete adicional para seguir procesando."
                        elif response.status_code == 500:
                            error_msg = f"Error del servidor: 500 - Error interno del servidor.\n\nDetalles: {error_content}\n\nIntenta nuevamente o contacta al administrador del sistema."
                        else:
                            error_msg = f"Error del servidor: {response.status_code} - {error_content}"
                        raise Exception(error_msg)
                    
                    # Obtener job_id de la respuesta
                    response_data = response.json()
                    job_id = response_data.get('job_id')
                    if not job_id:
                        raise Exception("No se recibió job_id del servidor")
                    
                    logger.info(f"Job enviado a la cola. Job ID: {job_id}")
                    
                    # Mensaje informativo estático (varias líneas, no se sale del cuadro)
                    _set_info(
                        f"Imagen enviada: {total_size_mb:.1f} MB\n"
                        "Revisando estado en servidor..."
                    )
                    _set_progress(45, "En la cola de procesamiento del servidor, por favor espere...")
                    
                    logger.info(f"Iniciando polling para job_id: {job_id}")
                    
                    # Polling del estado cada QUEUE_POLLING_INTERVAL segundos
                    polling_start_time = time.time()
                    last_status = None
                    last_message = None
                    last_progress_percent = -1
                    polling_count = 0
                    
                    while True:
                        if self.cancel_flag and self.cancel_flag.get("cancelled"):
                            logger.info("[THREAD] Cancelado por el usuario durante el polling")
                            segmentation_result["cancelled"] = True
                            segmentation_result["completed"] = True
                            return
                        # Verificar timeout: si el progreso es alto (>=80%) dar 5 min extra para no cortar cerca del final
                        extra_if_near_end = 300 if (last_progress_percent or 0) >= 80 else 0
                        effective_timeout = QUEUE_TIMEOUT + extra_if_near_end
                        if time.time() - polling_start_time > effective_timeout:
                            raise Exception(
                                f"Timeout: La segmentación superó el tiempo máximo de espera ({QUEUE_TIMEOUT // 60} min). "
                                "El trabajo puede seguir en proceso en el servidor; si estaba cerca de terminar, espere unos minutos e intente de nuevo."
                            )
                        
                        polling_count += 1
                        
                        # Consultar estado usando el job_id
                        status_url = f"{STATUS_ENDPOINT}/{job_id}"
                        logger.info(f"[THREAD] Consultando estado en: {status_url} (intento {polling_count})")
                        
                        try:
                            status_response = requests.get(status_url, headers=auth_headers, timeout=30)
                        except Exception as e:
                            logger.warning(f"Error consultando estado: {str(e)}")
                            elapsed_time = int(time.time() - polling_start_time)
                            _set_progress(45, f"En la cola de procesamiento del servidor, por favor espere... ({elapsed_time}s transcurridos)")
                            time.sleep(QUEUE_POLLING_INTERVAL)
                            continue

                        if status_response.status_code != 200:
                            logger.warning(f"Error consultando estado: {status_response.status_code} - {status_response.text}")
                            elapsed_time = int(time.time() - polling_start_time)
                            _set_progress(45, f"En la cola de procesamiento del servidor, por favor espere... ({elapsed_time}s transcurridos)")
                            time.sleep(QUEUE_POLLING_INTERVAL)
                            continue
                        
                        status_data = status_response.json()
                        current_status = status_data.get('status')
                        # Asegurar que progress/message no sean None (API puede devolver null)
                        current_message = (status_data.get('message') or '').strip() or 'Procesando...'
                        current_progress = status_data.get('progress')
                        if current_progress is None:
                            current_progress = 0
                        try:
                            current_progress = int(current_progress)
                        except (TypeError, ValueError):
                            current_progress = 0
                        
                        logger.info(f"Estado recibido: status={current_status}, progress={current_progress}, message={current_message}")

                        # Calcular tiempo transcurrido
                        elapsed_time = int(time.time() - polling_start_time)

                        # Recuadro prominente: fase real del procesamiento en el servidor
                        # (preparando imagen -> IA detectando palmas -> generando resultados)
                        _set_info(
                            f"Imagen enviada: {total_size_mb:.1f} MB\n"
                            f"{_friendly_processing_phase(current_progress)}"
                        )

                        # Mensaje inferior: solo tiempo transcurrido, sin repetir la fase ya
                        # mostrada arriba y sin mencionar "intentos" (se prestaba a pensar que
                        # la conexión estaba fallando).
                        display_message = f"En la cola de procesamiento del servidor, por favor espere... ({elapsed_time}s transcurridos)"

                        # Barra de progreso: determinada (45-55%) mientras el servidor solo está
                        # preparando la imagen; indeterminada (gif) durante la IA y la generación de
                        # resultados, ya que ahí el servidor solo reporta saltos gruesos e impredecibles.
                        if current_progress < 60:
                            bar_value = 45 + int(min(current_progress, 59) / 59 * 10)
                        else:
                            bar_value = None
                        _set_progress(bar_value, display_message)
                        last_message = current_message
                        last_status = current_status
                        last_progress_percent = current_progress
                        
                        if self.cancel_flag and self.cancel_flag.get("cancelled"):
                            logger.info("[THREAD] Cancelado por el usuario durante el polling")
                            segmentation_result["cancelled"] = True
                            segmentation_result["completed"] = True
                            return
                        # Verificar si está completado
                        if current_status == 'completed':
                            _set_progress(90, "Procesamiento completado. Descargando resultados...")
                            logger.info(f"Job {job_id} completado, descargando resultados...")
                            break
                        elif current_status == 'failed':
                            error_msg = status_data.get('error', 'Error desconocido')
                            raise Exception(f"Segmentación falló: {error_msg}")
                        
                        # Esperar antes de la siguiente consulta
                        time.sleep(QUEUE_POLLING_INTERVAL)
                    
                    if self.cancel_flag and self.cancel_flag.get("cancelled"):
                        logger.info("[THREAD] Cancelado por el usuario antes de descargar")
                        segmentation_result["cancelled"] = True
                        segmentation_result["completed"] = True
                        return
                    # Descargar resultado
                    _set_progress(90, "Descargando resultados del servidor...")
                    
                    result_url = f"{RESULT_ENDPOINT}/{job_id}"
                    result_response = None
                    max_result_retries = 20
                    result_retry_count = 0
                    
                    while result_retry_count < max_result_retries:
                        try:
                            logger.info(f"[THREAD] Intentando descargar resultado (intento {result_retry_count + 1}/{max_result_retries})")
                            result_response = requests.get(result_url, headers=auth_headers, timeout=300, stream=True)
                            
                            if result_response.status_code == 200:
                                logger.info(f"[THREAD] Resultado obtenido exitosamente")
                                break
                            
                            elif result_response.status_code == 202:
                                logger.info(f"[THREAD] Job aún finalizando, esperando {QUEUE_POLLING_INTERVAL} segundos...")
                                _set_progress(90, "Finalizando procesamiento, por favor espere...")
                                time.sleep(QUEUE_POLLING_INTERVAL)
                                result_retry_count += 1
                                continue
                            
                            else:
                                logger.warning(f"[THREAD] Código de respuesta inesperado: {result_response.status_code}")
                                if result_retry_count < max_result_retries - 1:
                                    time.sleep(QUEUE_POLLING_INTERVAL)
                                    result_retry_count += 1
                                    continue
                                else:
                                    error_detail = result_response.text
                                    try:
                                        error_json = result_response.json()
                                        error_detail = error_json.get('detail', error_detail)
                                    except:
                                        logging.getLogger(__name__).debug(
                                            "Fallo no crítico; se continúa.", exc_info=True)
                                    raise Exception(f"Error descargando resultado después de {max_result_retries} intentos: {result_response.status_code} - {error_detail}")
                        
                        except requests.exceptions.RequestException as e:
                            logger.warning(f"[THREAD] Error de conexión al descargar resultado: {str(e)}")
                            if result_retry_count < max_result_retries - 1:
                                time.sleep(QUEUE_POLLING_INTERVAL)
                                result_retry_count += 1
                                continue
                            else:
                                raise Exception(f"Error de conexión al descargar resultado después de {max_result_retries} intentos: {str(e)}")
                    
                    # Verificar que obtuvimos una respuesta exitosa
                    if result_response is None or result_response.status_code != 200:
                        raise Exception(f"No se pudo obtener el resultado después de {max_result_retries} intentos")
                    
                    # Crear directorio temporal
                    temp_dir = tempfile.mkdtemp(prefix="palm_segmentation_")
                    logger.info(f"Directorio temporal creado: {temp_dir}")
                    
                    zip_path = os.path.join(temp_dir, "segmented_palms.zip")
                    
                    # Descargar archivo con progreso
                    downloaded_size = 0
                    total_size = int(result_response.headers.get('Content-Length', 0))
                    
                    with open(zip_path, 'wb') as f:
                        for chunk in result_response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded_size += len(chunk)
                                if self.progress_callback and total_size > 0:
                                    downloaded_mb = downloaded_size / (1024*1024)
                                    total_mb = total_size / (1024*1024)
                                    download_bar_value = 90 + int((downloaded_size / total_size) * 7)
                                    _set_progress(download_bar_value, f"Descargando resultados... {downloaded_mb:.1f} MB de {total_mb:.1f} MB")
                    
                    logger.info(f"Archivo ZIP guardado: {zip_path}")
                    
                    # Descomprimir en la carpeta de la imagen
                    image_dir = os.path.dirname(image_path)
                    logger.info(f"Descomprimiendo archivos en: {image_dir}")
                    
                    # Mostrar progreso de extracción
                    _set_progress(97, "Extrayendo archivos de resultados...")
                    
                    # Lista para almacenar los archivos extraídos
                    extracted_files = []
                    mask_found = None
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        # Obtener lista de archivos en el ZIP
                        zip_file_list = zip_ref.namelist()
                        logger.info(f"Archivos en el ZIP: {zip_file_list}")

                        # Extraer todos los archivos
                        zip_ref.extractall(image_dir)

                        # Buscar el TIFF de máscara real (1 banda, liviano) si la API lo envió
                        for file_name in zip_file_list:
                            if file_name.lower().endswith('_mask.tif'):
                                mask_found = os.path.join(image_dir, file_name)
                                logger.info(f"TIFF de máscara encontrado en ZIP: {file_name}")
                                break

                        # Buscar el TIFF RGB de segmentación (preferir *_rgb.tif explícito)
                        tiff_found = None
                        for file_name in zip_file_list:
                            if file_name.lower().endswith('_rgb.tif'):
                                tiff_found = os.path.join(image_dir, file_name)
                                logger.info(f"TIFF RGB de segmentación encontrado en ZIP: {file_name}")
                                break

                        # Compatibilidad con respuestas antiguas: cualquier .tif que no sea la máscara
                        if not tiff_found:
                            for file_name in zip_file_list:
                                if file_name.lower().endswith(('.tif', '.tiff')) and not file_name.lower().endswith('_mask.tif'):
                                    tiff_found = os.path.join(image_dir, file_name)
                                    logger.info(f"TIFF de segmentación encontrado en ZIP: {file_name}")
                                    break

                        # Si no se encontró un TIFF (y tampoco hay máscara), buscar cualquier
                        # archivo de imagen que no sea la máscara
                        if not tiff_found and not mask_found:
                            for file_name in zip_file_list:
                                if file_name.lower().endswith(('.tif', '.tiff', '.png', '.jpg', '.jpeg')):
                                    tiff_found = os.path.join(image_dir, file_name)
                                    logger.info(f"Archivo de imagen encontrado en ZIP (fallback): {file_name}")
                                    break

                    # Se pidió include_rgb_output=false: es normal y esperado que el ZIP solo
                    # traiga la máscara (sin TIFF RGB) — basta con que exista alguno de los dos.
                    if (not tiff_found or not os.path.exists(tiff_found)) and (not mask_found or not os.path.exists(mask_found)):
                        raise Exception(f"No se encontró ningún archivo de imagen ni de máscara en el archivo ZIP")

                    if tiff_found:
                        logger.info(f"TIFF de segmentación extraído: {tiff_found}")
                    if mask_found:
                        logger.info(f"Máscara real extraída: {mask_found}")

                    # Mostrar progreso de procesamiento
                    if self.progress_callback:
                        _set_progress(98, "Procesando archivos de resultados...")

                    # Crear carpeta de resultados con FID y timestamp. Si el usuario eligió
                    # una carpeta de resultados propia, se guarda ahí (subcarpeta por lote);
                    # si no, se mantiene el comportamiento de siempre: junto a la ortoimagen.
                    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                    base_dir = output_folder if output_folder and os.path.isdir(output_folder) else image_dir
                    output_dir = os.path.join(base_dir, f"segmentador_palmas_FID{lot_id_normalized}_{timestamp}")
                    os.makedirs(output_dir, exist_ok=True)
                    logger.info(f"Directorio de resultados creado: {output_dir}")

                    resultado_rgb_path = os.path.join(output_dir, "ResultadoRGB.tif")

                    # Generar TIFF multibanda enmascarado y guardar como ResultadoRGB.tif
                    multiband_out = None
                    if multiband_clip_for_indices and os.path.exists(multiband_clip_for_indices):
                        try:
                            multiband_out = _apply_mask_to_multiband(
                                tiff_found, multiband_clip_for_indices, resultado_rgb_path,
                                mask_path=mask_found
                            )
                        except Exception as _mb_err:
                            logger.warning(f"No se pudo generar TIFF multibanda: {_mb_err}")

                    # Fallback: copiar TIFF RGB de la API como ResultadoRGB.tif (solo si llegó uno;
                    # con include_rgb_output=false normalmente no hay RGB de respaldo, así que si
                    # _apply_mask_to_multiband falló aquí no hay nada más que intentar).
                    if (not multiband_out or not os.path.exists(resultado_rgb_path)):
                        if tiff_found and os.path.exists(tiff_found):
                            shutil.copy2(tiff_found, resultado_rgb_path)
                        else:
                            raise Exception(
                                "No se pudo generar el resultado: falló el enmascarado con la "
                                "máscara real y la API no devolvió un TIFF RGB de respaldo."
                            )

                    output_shapefile = resultado_rgb_path
                    segmentation_result['multiband_segmented_path'] = resultado_rgb_path
                    self.last_multiband_segmented_path = resultado_rgb_path
                    logger.info(f"Archivo resultado: {output_shapefile}")

                    # Limpiar archivos crudos extraídos del ZIP (tiff_found, mask_found):
                    # quedaban sueltos junto al ortomosaico original (image_dir) en vez de
                    # vivir solo dentro de la carpeta de resultados (output_dir). Ya se usaron
                    # para generar resultado_rgb_path, así que no hacen falta más.
                    if os.path.exists(resultado_rgb_path):
                        for _raw_path in (tiff_found, mask_found):
                            if _raw_path and os.path.exists(_raw_path):
                                try:
                                    os.remove(_raw_path)
                                    logger.info(f"Archivo crudo temporal eliminado: {_raw_path}")
                                except Exception as _cleanup_err:
                                    logger.warning(f"No se pudo eliminar archivo crudo temporal {_raw_path}: {_cleanup_err}")

                    # Mostrar progreso final
                    _set_progress(100, "Procesamiento completado exitosamente")
                    
                    # Guardar la ruta del archivo actual
                    self.last_output_shapefile = output_shapefile
                    logger.info(f"Segmentación completada exitosamente")
                    
                    # Actualizar resultado para el hilo principal
                    segmentation_result['output_shapefile'] = output_shapefile
                    segmentation_result['completed'] = True
                    
                    return output_shapefile
                except Exception as e:
                    error_msg = str(e)
                    error_traceback = traceback.format_exc()
                    logger.error(f"[THREAD] Error en la segmentación: {error_msg}")
                    logger.error(f"[THREAD] Traceback completo:\n{error_traceback}")
                    
                    # Construir mensaje de error más informativo
                    if job_id:
                        error_detail = f"Error durante el procesamiento del job {job_id}: {error_msg}"
                    else:
                        error_detail = f"Error durante el procesamiento: {error_msg}"
                    
                    # Si el error contiene información específica, usarla
                    if "Timeout" in error_msg:
                        error_detail = (
                            f"{error_msg} "
                            "El trabajo puede seguir en proceso en el servidor; si estaba cerca de terminar, espere unos minutos e intente de nuevo."
                        )
                    elif "Error descargando resultado" in error_msg:
                        error_detail = f"Error al obtener los resultados del servidor. {error_msg}"
                    elif "Error consultando estado" in error_msg:
                        error_detail = f"Error al consultar el estado del proceso. {error_msg}"
                    
                    _set_progress(0, f"Error: {error_detail}")
                    segmentation_result['error'] = error_detail
                    segmentation_result['completed'] = True
            
            # Variables para comunicación entre hilos
            segmentation_result = {
                'output_shapefile': None, 'error': None, 'completed': False, 'cancelled': False,
                'progress_update': None, 'info_update': None,
            }

            def _set_progress(value, message):
                """
                Escribe el progreso desde el hilo de fondo (segmentation_thread). No se puede usar
                safe_progress_callback aquí: QTimer.singleShot requiere un event loop de Qt en el
                hilo que lo invoca, y este es un threading.Thread plano sin event loop, así que el
                timer nunca se dispara. El while de más abajo (que sí corre en el hilo principal
                de Qt) lee este valor y lo aplica directamente.
                """
                segmentation_result['progress_update'] = (value, message)

            def _set_info(message):
                segmentation_result['info_update'] = message

            # Función para manejar la finalización del hilo
            def on_segmentation_complete():
                segmentation_result['completed'] = True
            
            # Iniciar el hilo de segmentación
            logger.info(f"Iniciando hilo de segmentación")
            segmentation_thread = threading.Thread(target=segmentation_thread)
            segmentation_thread.daemon = True  # Hilo daemon para que no bloquee la aplicación
            segmentation_thread.start()
            
            # Mostrar mensaje inicial inmediatamente
            if self.progress_callback:
                try:
                    self.progress_callback(15, "Preparando archivos para segmentación...")
                    QApplication.processEvents()
                except Exception as e:
                    logger.error(f"Error al mostrar mensaje inicial: {str(e)}")
            
            # Esperar a que termine el hilo de segmentación sin bloquear la interfaz
            logger.info(f"Esperando a que termine el hilo de segmentación (timeout {QUEUE_TIMEOUT + 120}s)")
            timeout_seconds = QUEUE_TIMEOUT + 120  # un poco más que el hilo para que sea el hilo quien decida
            start_time = time.time()
            
            last_dispatched_progress = None
            last_dispatched_info = None
            while not segmentation_result['completed'] and segmentation_thread.is_alive():
                # Verificar timeout (el hilo tiene su propio QUEUE_TIMEOUT; este evita esperar indefinidamente si el hilo se cuelga)
                if time.time() - start_time > timeout_seconds:
                    logger.error("Timeout esperando la segmentación")
                    raise Exception(
                        "Timeout: La segmentación tardó demasiado tiempo. "
                        "El trabajo puede seguir en proceso en el servidor; espere unos minutos y puede intentar de nuevo."
                    )

                # Propagar a la UI el progreso/info que el hilo de fondo fue dejando en segmentation_result.
                # Esto corre en el hilo principal de Qt, así que las llamadas directas sí se aplican
                # (a diferencia de safe_progress_callback/safe_info_callback llamadas desde el hilo de fondo).
                progress_update = segmentation_result.get('progress_update')
                if progress_update and progress_update != last_dispatched_progress and self.progress_callback:
                    self.progress_callback(progress_update[0], progress_update[1])
                    last_dispatched_progress = progress_update

                info_update = segmentation_result.get('info_update')
                if info_update and info_update != last_dispatched_info and self.info_callback:
                    self.info_callback(info_update)
                    last_dispatched_info = info_update

                QApplication.processEvents()  # Mantener la interfaz responsiva
                time.sleep(0.1)  # Pequeña pausa para no saturar la CPU
            
            # Verificar si el usuario canceló
            if segmentation_result.get('cancelled'):
                raise SegmentationCancelled("Proceso cancelado por el usuario")
            # Verificar si el hilo terminó correctamente
            if not segmentation_result['completed']:
                logger.error("El hilo de segmentación no se completó correctamente")
                raise Exception("El proceso de segmentación no se completó")
            # Verificar si hubo error en el hilo
            if segmentation_result['error']:
                raise Exception(segmentation_result['error'])
            
            # Obtener el resultado del hilo
            output_shapefile = segmentation_result['output_shapefile']
            
            # Verificar que el proceso se completó correctamente
            if not output_shapefile or not os.path.exists(output_shapefile):
                logger.error(f"No se pudo generar el archivo de resultados")
                raise Exception("No se pudo generar el archivo de resultados")
            
            # Limpiar archivos temporales
            # Limpiar el directorio temporal si existe
            if temp_dir and isinstance(temp_dir, str) and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Directorio temporal eliminado: {temp_dir}")
                except Exception as e:
                    logger.error(f"Error al eliminar directorio temporal: {str(e)}")
            
            # Limpiar archivos temporales del clip
            if clipped_raster_path and os.path.exists(clipped_raster_path):
                try:
                    clipped_dir = os.path.dirname(clipped_raster_path)
                    if os.path.exists(clipped_dir):
                        shutil.rmtree(clipped_dir)
                        logger.info(f"Directorio del raster recortado eliminado: {clipped_dir}")
                except Exception as e:
                    logger.warning(f"Error al eliminar directorio del raster recortado: {str(e)}")
            
            if temp_lote_shapefile and os.path.exists(temp_lote_shapefile):
                try:
                    lote_dir = os.path.dirname(temp_lote_shapefile)
                    if os.path.exists(lote_dir):
                        shutil.rmtree(lote_dir)
                        logger.info(f"Directorio del shapefile temporal eliminado: {lote_dir}")
                except Exception as e:
                    logger.warning(f"Error al eliminar directorio del shapefile temporal: {str(e)}")
            
            logger.info(f"Proceso completado exitosamente")
            return output_shapefile
                    
        except Exception as e:
            # Limpiar archivos temporales en caso de error
            # Limpiar el directorio temporal si existe
            if temp_dir and isinstance(temp_dir, str) and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Directorio temporal eliminado después de error: {temp_dir}")
                except Exception as cleanup_error:
                    logger.error(f"Error al eliminar directorio temporal: {str(cleanup_error)}")
            
            # Limpiar archivos temporales del clip en caso de error
            if clipped_raster_path and os.path.exists(clipped_raster_path):
                try:
                    clipped_dir = os.path.dirname(clipped_raster_path)
                    if os.path.exists(clipped_dir):
                        shutil.rmtree(clipped_dir)
                        logger.info(f"Directorio del raster recortado eliminado después de error: {clipped_dir}")
                except Exception as cleanup_error:
                    logger.warning(f"Error al eliminar directorio del raster recortado: {str(cleanup_error)}")
            
            if temp_lote_shapefile and os.path.exists(temp_lote_shapefile):
                try:
                    lote_dir = os.path.dirname(temp_lote_shapefile)
                    if os.path.exists(lote_dir):
                        shutil.rmtree(lote_dir)
                        logger.info(f"Directorio del shapefile temporal eliminado después de error: {lote_dir}")
                except Exception as cleanup_error:
                    logger.warning(f"Error al eliminar directorio del shapefile temporal: {str(cleanup_error)}")
            
            logger.error(f"Error en la segmentación: {str(e)}")
            raise Exception(f"Error en la segmentación: {str(e)}")


def run_segmentation_headless(image_path, lotes_path, lot_id, slice_height=None, slice_width=None,
                               overlap_ratio=None, confidence_threshold=None,
                               progress_callback=None, info_callback=None, progress_dialog=None,
                               output_folder=None):
    """
    Ejecuta una segmentación sin UI: valida rutas/parámetros, llama a LocalSegmentador y
    carga el TIFF resultante en el proyecto de QGIS. No abre ProgressDialog ni
    QMessageBox — pensada para invocarse desde el Asistente (chat) o cualquier
    automatización, con o sin un dockwidget/panel abierto.

    Retorna dict: {"layer_name", "total_segments", "output_shapefile", "multiband_segmented_path"}.
    """
    slice_height = slice_height if slice_height is not None else DEFAULT_SLICE_HEIGHT
    slice_width = slice_width if slice_width is not None else DEFAULT_SLICE_WIDTH
    overlap_ratio = overlap_ratio if overlap_ratio is not None else DEFAULT_OVERLAP_RATIO
    confidence_threshold = confidence_threshold if confidence_threshold is not None else DEFAULT_CONFIDENCE_THRESHOLD
    standard_resolution = DEFAULT_STANDARD_RESOLUTION
    res_threshold = DEFAULT_RES_THRESHOLD

    if not image_path or not os.path.exists(image_path):
        raise Exception("No se encontró la ortoimagen")
    # Cualquier ráster que GDAL/QGIS pueda abrir sirve (.tif, .ecw, .img, .jp2, .sid, ...).
    _check_layer = QgsRasterLayer(image_path, "check_ortoimagen")
    if not _check_layer.isValid():
        raise Exception(
            "No se pudo abrir la ortoimagen. Verifique que sea un ráster georreferenciado "
            "válido (GeoTIFF, ECW, IMG, JP2, etc.)."
        )
    del _check_layer

    if not lotes_path or not os.path.exists(lotes_path):
        raise Exception("No se encontró el shapefile de lotes")
    if not lotes_path.lower().endswith('.shp'):
        raise Exception("El archivo de lotes debe ser un shapefile (.shp)")

    base_path = os.path.splitext(lotes_path)[0]
    missing_files = []
    for ext in ['.dbf', '.shx', '.prj']:
        if not os.path.exists(base_path + ext):
            missing_files.append(ext)
    if missing_files:
        raise Exception(f"Faltan archivos necesarios del shapefile: {', '.join(missing_files)}")

    logger.info(f"Iniciando nueva segmentación - Lote ID: {lot_id}")
    logger.info(f"Imagen: {image_path}")
    logger.info(f"Shapefile: {lotes_path}")

    cancel_flag = {"cancelled": False}
    segmentador = LocalSegmentador(progress_callback=progress_callback, info_callback=info_callback, cancel_flag=cancel_flag)
    if progress_dialog is not None:
        segmentador.progress_dialog = progress_dialog
    output_shapefile = segmentador.process_segmentation(
        image_path, lotes_path, lot_id,
        slice_height=slice_height,
        slice_width=slice_width,
        overlap_ratio=overlap_ratio,
        confidence_threshold=confidence_threshold,
        standard_resolution=standard_resolution,
        res_threshold=res_threshold,
        output_folder=output_folder
    )
    multiband_segmented_path = getattr(segmentador, 'last_multiband_segmented_path', None) or output_shapefile

    if not os.path.exists(output_shapefile):
        raise Exception(f"No se encontró el archivo de resultados: {output_shapefile}")
    if not output_shapefile.lower().endswith(('.tif', '.tiff')):
        raise Exception("El archivo de resultados no es un archivo TIFF válido")

    layer_name = os.path.basename(output_shapefile).replace('.tif', '').replace('.tiff', '')
    logger.info(f"Cargando capa raster con nombre: {layer_name}")
    rlayer = QgsRasterLayer(output_shapefile, layer_name, "gdal")

    if not rlayer.isValid():
        error_msg = f"La capa raster no es válida. Detalles:\n"
        error_msg += f"- Ruta: {output_shapefile}\n"
        error_msg += f"- Driver: {rlayer.providerType()}\n"
        error_msg += f"- Error: {rlayer.error().message() if rlayer.error() else 'Desconocido'}"
        raise Exception(error_msg)

    if rlayer.width() == 0 or rlayer.height() == 0:
        raise Exception("La capa raster no contiene datos válidos")
    if not rlayer.crs().isValid():
        raise Exception("La capa raster no tiene un sistema de coordenadas válido")

    QgsProject.instance().addMapLayer(rlayer)

    total_segments = f"{rlayer.width()}x{rlayer.height()} píxeles"
    logger.info(f"Segmentación completada: {total_segments}")
    logger.info(f"TIFF {output_shapefile} guardado y agregado al panel de capas")

    return {
        "layer_name": layer_name,
        "total_segments": total_segments,
        "output_shapefile": output_shapefile,
        "multiband_segmented_path": multiband_segmented_path,
    }


def run_segmentation(dockwidget):
    """
    Orquesta una segmentación con UI (ProgressDialog, QMessageBox) sobre `dockwidget`,
    delegando el trabajo real a run_segmentation_headless().

    Se invoca directamente desde SegmentadorPalmasDockWidget una vez que ya validó
    autenticación, selección de lote y traslape lote/imagen.
    """
    # Carpeta de resultados obligatoria: sin ella, antes el resultado terminaba
    # junto a la ortoimagen sin que el usuario lo decidiera explícitamente.
    output_folder_check = dockwidget.outputFolderEdit.text().strip() if hasattr(dockwidget, 'outputFolderEdit') else ""
    if not output_folder_check:
        QMessageBox.warning(
            dockwidget, "Falta la carpeta de resultados",
            "Selecciona primero una carpeta de resultados (arriba del todo del panel) "
            "antes de iniciar la segmentación."
        )
        return
    if not os.path.isdir(output_folder_check):
        QMessageBox.critical(
            dockwidget, "Carpeta de resultados no válida",
            "La carpeta de resultados elegida ya no existe. Selecciona una carpeta válida antes de continuar."
        )
        return

    progress_dialog = None
    lot_id = getattr(dockwidget, 'lot_id', None)
    try:
        image_path = dockwidget.lineEdit.text()
        lotes_path = dockwidget.lineEdit_2.text()

        slice_height = int(dockwidget.lineEdit_slice_height.value()) if hasattr(dockwidget, 'lineEdit_slice_height') else None
        slice_width = int(dockwidget.lineEdit_slice_width.value()) if hasattr(dockwidget, 'lineEdit_slice_width') else None
        if not hasattr(dockwidget, 'lineEdit_slice_height') and hasattr(dockwidget, 'lineEdit_slice_size'):
            slice_size = int(dockwidget.lineEdit_slice_size.value())
            slice_height = slice_size
            slice_width = slice_size
        overlap_ratio = float(dockwidget.lineEdit_overlap.value()) if hasattr(dockwidget, 'lineEdit_overlap') else None
        confidence_threshold = float(dockwidget.lineEdit_confidence.value()) if hasattr(dockwidget, 'lineEdit_confidence') else None

        # Crear y mostrar ventana de progreso (cancel_flag compartido: el hilo lo comprueba y sale si el usuario cancela)
        cancel_flag = {"cancelled": False}
        progress_dialog = ProgressDialog(dockwidget, cancel_flag=cancel_flag)
        progress_dialog.show()
        progress_dialog.raise_()
        progress_dialog.activateWindow()
        progress_dialog.set_info_message("Por favor espere. Esta operación puede tardar varios minutos.")
        progress_dialog.update_progress(0, "Preparando archivos para segmentación...")
        QApplication.processEvents()

        def update_progress(value, message):
            try:
                logger.debug(f"Actualizando progreso: {value}% - {message}")
                if progress_dialog:
                    if not progress_dialog.isVisible():
                        progress_dialog.show()
                        progress_dialog.raise_()
                        progress_dialog.activateWindow()
                    progress_dialog.update_progress(value, message)
                    QApplication.processEvents()
                    if value == 100:
                        progress_dialog.raise_()
                        progress_dialog.activateWindow()
                        QApplication.processEvents()
            except Exception as e:
                logger.error(f"Error al actualizar progreso: {str(e)}")
                try:
                    if progress_dialog:
                        progress_dialog.message_label.setText(str(message))
                        QApplication.processEvents()
                except Exception as e2:
                    logger.error(f"Error crítico al actualizar progreso: {str(e2)}")

        def update_info(msg):
            try:
                if progress_dialog and hasattr(progress_dialog, 'set_info_message'):
                    progress_dialog.set_info_message(msg)
                    QApplication.processEvents()
            except Exception as e:
                logger.error(f"Error al actualizar info: {str(e)}")

        output_folder = dockwidget.outputFolderEdit.text().strip() if hasattr(dockwidget, 'outputFolderEdit') else None

        result = run_segmentation_headless(
            image_path, lotes_path, lot_id,
            slice_height=slice_height, slice_width=slice_width,
            overlap_ratio=overlap_ratio, confidence_threshold=confidence_threshold,
            progress_callback=update_progress, info_callback=update_info,
            progress_dialog=progress_dialog,
            output_folder=output_folder,
        )
        layer_name = result["layer_name"]
        total_segments = result["total_segments"]
        output_shapefile = result["output_shapefile"]
        multiband_segmented_path = result["multiband_segmented_path"]

        if hasattr(dockwidget, 'show_spectral_index_button'):
            dockwidget.show_spectral_index_button(multiband_segmented_path, image_path)

        filename = os.path.basename(output_shapefile)
        final_message = (
            "✅ <b>¡SEGMENTACIÓN COMPLETADA!</b><br><br>"
            f"<b>Resolución del raster:</b> <span style='color:green;font-size:18px'>{total_segments}</span><br>"
            f"<b>Capa agregada:</b> <i>{layer_name}</i><br>"
            f"<b>Archivo:</b> {filename}<br>"
        )
        progress_dialog.raise_()
        progress_dialog.activateWindow()

        for i in range(3):
            update_progress(100, final_message)
            QApplication.processEvents()
            time.sleep(0.1)

        update_progress(100, "Segmentación completada exitosamente. Puedes cerrar esta ventana.")
        QApplication.processEvents()
        time.sleep(0.5)

        success_message = (
            f"Segmentación completada exitosamente\n\n"
            f"Resolución del raster: {total_segments}\n\n"
            f"La capa raster '{layer_name}' fue agregada al proyecto."
        )
        QMessageBox.information(dockwidget, "Segmentación Completada", success_message)

    except SegmentationCancelled:
        logger.info("Proceso de segmentación cancelado por el usuario")
        if progress_dialog:
            try:
                progress_dialog.close()
            except Exception:
                logging.getLogger(__name__).debug(
                    "Fallo no crítico; se continúa.", exc_info=True)
        QMessageBox.information(dockwidget, "Proceso cancelado", "La segmentación fue cancelada. Puede iniciar una nueva cuando lo desee.")
        return

    except Exception as e:
        error_message = f"Error en lote {lot_id if lot_id else 'desconocido'}: {str(e)}"
        logger.error(error_message)
        if progress_dialog:
            progress_dialog.update_progress(0, f"❌ ERROR\n\n{error_message}")
            QApplication.processEvents()
            time.sleep(4)
        QMessageBox.critical(dockwidget, "Error", error_message)
    finally:
        if progress_dialog:
            QApplication.processEvents()
            time.sleep(0.5)
            progress_dialog.close()

