# -*- coding: utf-8 -*-
import os
import json
import platform
from uuid import uuid4
import requests
import tempfile
import zipfile
import shutil
import threading
import time
from qgis.PyQt.QtCore import QSettings, QCoreApplication, Qt, QTranslator, QTimer, QThread, pyqtSignal, QMetaObject
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import (QAction, QMessageBox, QApplication,
                                QLabel)
from qgis.core import QgsProject, QgsVectorLayer, QgsRasterLayer, QgsProcessingFeedback, QgsProcessingUtils, QgsApplication, QgsProcessingContext, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, Qgis, QgsField, QgsMapLayer, QgsSymbol, QgsSingleSymbolRenderer
from qgis.PyQt.QtCore import QVariant
from .dockwidget import ProgressDialog
# Detection logger removido - se maneja desde la API
import logging
import sys
import traceback
from datetime import datetime

# Importar configuración (nombres genéricos aliasados desde el config.py unificado)
from ..config import (API_BASE_URL, DETECT_PALMS_ENDPOINT, DETECT_PALMS_ASYNC_ENDPOINT,
                    DETECTOR_STATUS_ENDPOINT as STATUS_ENDPOINT,
                    DETECTOR_RESULT_ENDPOINT as RESULT_ENDPOINT,
                    DETECTOR_QUEUE_STATUS_ENDPOINT as QUEUE_STATUS_ENDPOINT,
                    DETECTOR_PROGRESS_ENDPOINT as PROGRESS_ENDPOINT,
                    DETECTOR_QUEUE_POLLING_INTERVAL as QUEUE_POLLING_INTERVAL,
                    DETECTOR_QUEUE_TIMEOUT as QUEUE_TIMEOUT,
                    UPLOAD_METHOD, UPLOAD_TIMEOUT, UPLOAD_PROGRESS_INTERVAL, API_KEY)


def _cabeceras_autenticacion():
    """Cabeceras para hablar con el backend (ver client_identity)."""
    from ..client_identity import cabeceras_autenticacion
    return cabeceras_autenticacion()


PENDIENTES_DIR = os.path.join(os.path.expanduser("~"), ".toolkitpalm", "pendientes")

# Los bloques de un lote grande se procesan en paralelo (ver process_detection_smart).
# La preparación local (leer el shapefile, recortar el raster) usa QGIS/GDAL, que no
# es seguro ejecutar simultáneamente desde varios hilos: se serializa con este candado.
# La parte lenta —subir, esperar al servidor y descargar— sí corre en paralelo.
_PREPARACION_LOCK = threading.RLock()


def _procesar_eventos_si_hilo_principal():
    """Refresca la interfaz solo si estamos en el hilo principal de Qt."""
    try:
        app = QApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            QApplication.processEvents()
    except Exception:
        logging.getLogger(__name__).debug(
            "Fallo no crítico; se continúa.", exc_info=True)


def _ruta_estado_pendiente(lotes_path, lot_id):
    """Archivo donde se guarda el avance de un lote que quedó a medias.

    Vive en la carpeta del usuario (no en la del proyecto) para que sobreviva
    aunque se cambie la carpeta de resultados entre corridas.
    """
    import hashlib
    # Resumen corto del lote para nombrar el archivo de avance. No cumple
    # ninguna función de seguridad: solo evita que dos lotes distintos
    # escriban en el mismo archivo.
    clave = hashlib.sha256(
        f"{os.path.abspath(lotes_path)}|{lot_id}".encode("utf-8")).hexdigest()[:12]
    return os.path.join(PENDIENTES_DIR, f"lote_{lot_id}_{clave}.json")


def leer_estado_pendiente(lotes_path, lot_id):
    """Devuelve el avance guardado de un lote incompleto, o None si no hay."""
    try:
        ruta = _ruta_estado_pendiente(lotes_path, lot_id)
        if not os.path.exists(ruta):
            return None
        with open(ruta, "r", encoding="utf-8") as f:
            estado = json.load(f)
        # Descartar el estado si los resultados parciales ya no existen en disco
        estado["resultados"] = [p for p in estado.get("resultados", []) if os.path.exists(p)]
        if not estado.get("pendientes_wkt"):
            return None
        return estado
    except Exception as e:
        logger.warning(f"No se pudo leer el estado pendiente: {e}")
        return None


def borrar_estado_pendiente(lotes_path, lot_id):
    """Borra el avance guardado y los shapefiles parciales que lo acompañan."""
    try:
        ruta = _ruta_estado_pendiente(lotes_path, lot_id)
        if os.path.exists(ruta):
            os.remove(ruta)
        carpeta = os.path.join(PENDIENTES_DIR, f"lote_{lot_id}")
        if os.path.isdir(carpeta):
            shutil.rmtree(carpeta, ignore_errors=True)
    except Exception as e:
        logger.warning(f"No se pudo borrar el estado pendiente: {e}")


def _compress_raster_if_needed(raster_path, umbral_mb=12.0, progress_callback=None):
    """
    Re-comprime el recorte con LZW si pesa más de `umbral_mb`.

    Red de seguridad independiente de qué método haya hecho el recorte: los
    algoritmos de QGIS Processing a veces ignoran las opciones de creación y
    devuelven el TIFF sin comprimir (30MB+), lo que hace la subida lentísima y
    puede superar el límite de tamaño del backend. No cambia resolución ni datos,
    solo la codificación del archivo. Devuelve la ruta a usar.
    """
    try:
        if not raster_path or not os.path.exists(raster_path):
            return raster_path
        size_mb = os.path.getsize(raster_path) / (1024 * 1024)
        if size_mb <= umbral_mb:
            return raster_path
        from osgeo import gdal
        if progress_callback:
            progress_callback(16, f"Comprimiendo imagen ({size_mb:.1f} MB) antes de enviar...")
            QApplication.processEvents()
        out_path = os.path.join(os.path.dirname(raster_path), "clip_compressed.tif")
        ds = gdal.Translate(out_path, raster_path,
                            creationOptions=["COMPRESS=LZW", "TILED=YES"])
        if ds is None:
            return raster_path
        ds = None
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            nuevo_mb = os.path.getsize(out_path) / (1024 * 1024)
            logger.info(f"Recorte re-comprimido: {size_mb:.1f} MB -> {nuevo_mb:.1f} MB")
            if nuevo_mb < size_mb:
                return out_path
    except Exception as e:
        logger.warning(f"No se pudo re-comprimir el recorte: {e}; se envía tal cual")
    return raster_path


def _ensure_rgb_raster(raster_path, progress_callback=None):
    """
    Si el raster tiene más de 3 bandas, genera un TIFF de solo 3 bandas (RGB) para evitar
    fallos en detección. Retorna la ruta del archivo a usar (nuevo o el mismo).
    """
    try:
        import rasterio
    except ImportError:
        return raster_path
    try:
        if progress_callback:
            progress_callback(16, "Comprobando bandas de la imagen...")
            QApplication.processEvents()
        with rasterio.open(raster_path) as src:
            if src.count <= 3:
                return raster_path
            logger.info(f"Raster tiene {src.count} bandas; generando versión RGB (3 bandas) para detección")
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


# Initialize Qt resources from file resources.py
from ..resources import *

# Configurar logging con ruta segura
def setup_logging():
    """Configura el logging con una ruta segura en el directorio del usuario."""
    handlers = [logging.StreamHandler(sys.stdout)]
    
    try:
        # Intentar usar el directorio del plugin primero
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(plugin_dir, 'client.log')
        
        # Si no se puede escribir en el directorio del plugin, usar el directorio temporal del usuario
        try:
            # Probar si podemos escribir en el directorio del plugin
            test_file = os.path.join(plugin_dir, '.write_test')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
        except (PermissionError, OSError):
            # Si no se puede escribir, usar el directorio temporal del usuario
            user_temp_dir = os.path.join(os.path.expanduser('~'), '.qgis_detector_palmas')
            os.makedirs(user_temp_dir, exist_ok=True)
            log_file = os.path.join(user_temp_dir, 'client.log')
        
        # Agregar FileHandler solo si podemos crear el archivo
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        handlers.append(file_handler)
    except (PermissionError, OSError) as e:
        # Si no se puede crear el archivo de log, solo usar StreamHandler
        # Esto evita que el plugin falle por problemas de permisos
        pass
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )

# Configurar logging
setup_logging()
logger = logging.getLogger(__name__)

# Detection logger se maneja desde la API

PLUGIN_NAME = 'DetectorPalmas'

def nombre_seguro(texto, por_defecto="lote"):
    """Convierte un nombre en algo usable como archivo o carpeta."""
    limpio = "".join(c for c in (texto or "").strip() if c.isalnum() or c in ("_", "-", " "))
    limpio = "_".join(limpio.split())
    return limpio or por_defecto


def carpeta_del_lote(output_folder, lot_name):
    """Subcarpeta propia para cada lote dentro de la carpeta de resultados.

    Todo lo de un mismo lote (detección, palmas numeradas y líneas) queda junto,
    que es como se revisa después: por lote, no por tipo de archivo. Devuelve
    None si no hay carpeta de resultados configurada."""
    if not output_folder:
        return None
    destino = os.path.join(output_folder, nombre_seguro(lot_name))
    try:
        # También se crea la carpeta de resultados si todavía no existe: si el
        # usuario la eligió, la intención es guardar ahí. Antes, si no existía,
        # el resultado terminaba en la carpeta temporal sin avisar.
        os.makedirs(destino, exist_ok=True)
    except OSError as e:
        logger.warning(f"No se pudo crear la carpeta del lote: {e}")
        return None
    return destino


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

class LocalDetector:
    def __init__(self, api_url=None, progress_callback=None, dockwidget=None):
        # URL de la API en producción
        self.api_url = api_url or API_BASE_URL
        self.progress_callback = progress_callback
        self.dockwidget = dockwidget
        self.last_output_shapefile = None
        logger.info(f"Inicializando LocalDetector con API: {self.api_url}")
    
    def safe_progress_callback(self, value, message):
        """
        Ejecuta el callback de progreso de forma segura desde cualquier hilo.
        Usa QTimer para ejecutar en el hilo principal de Qt.
        """
        if self.progress_callback:
            try:
                # Usar QTimer.singleShot para ejecutar en el hilo principal
                QTimer.singleShot(0, lambda: self.progress_callback(value, message))
            except Exception as e:
                logger.error(f"Error en safe_progress_callback: {str(e)}")
    
    def verify_server_connection(self):
        """
        Verifica si el servidor está funcionando
        """
        try:
            import requests
            # Intentar con diferentes endpoints de salud
            health_endpoints = ["/health", "/", "/status", "/api/health"]
            
            for endpoint in health_endpoints:
                try:
                    response = requests.get(f"{self.api_url}{endpoint}", timeout=10)
                    if response.status_code == 200:
                        logger.info(f"Servidor está funcionando correctamente (endpoint: {endpoint})")
                        return True
                    else:
                        logger.info(f"Endpoint {endpoint} respondió con código: {response.status_code}")
                except Exception as e:
                    logger.info(f"Endpoint {endpoint} no disponible: {str(e)}")
                    continue
            
            # Si ningún endpoint de salud funciona, intentar con el endpoint principal
            try:
                response = requests.get(f"{self.api_url}/detect_palms/", timeout=10)
                logger.info(f"Endpoint principal respondió con código: {response.status_code}")
                return True  # El servidor está funcionando, aunque el endpoint no sea de salud
            except Exception as e:
                logger.error(f"Error al verificar servidor: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"Error al verificar servidor: {str(e)}")
            return False

    def calculate_lot_area_hectares(self, geometry, crs):
        """
        Calcula el área del lote en hectáreas usando medición geodésica
        con `QgsDistanceArea` para evitar valores 0.00 cuando el CRS es
        geográfico o no proyectado.
        """
        try:
            distance_area = QgsDistanceArea()
            # Intentar usar el elipsoide del proyecto para mayor precisión
            try:
                distance_area.setEllipsoid(QgsProject.instance().ellipsoid())
            except Exception:
                logging.getLogger(__name__).debug(
                    "Fallo no crítico; se continúa.", exc_info=True)

            # Tomar CRS de origen válido; si no, usar CRS del proyecto
            source_crs = crs if crs and crs.isValid() else QgsProject.instance().crs()
            distance_area.setSourceCrs(source_crs, QgsProject.instance().transformContext())
            # setEllipsoidalMode() ya no existe en QGIS 3.34+ (API cambió); basta con
            # setEllipsoid() arriba para que measureArea() use cálculo elipsoidal —
            # antes esta línea lanzaba AttributeError en cada llamada, silenciado por
            # el except de abajo, y el área del lote SIEMPRE llegaba como None (0 ha
            # al servidor, de ahí que el cobro de créditos nunca reflejara el tamaño real).

            area_sq_meters = distance_area.measureArea(geometry)
            area_hectares = area_sq_meters / 10000.0
            logger.info(f"Área medida: {area_sq_meters:.2f} m² -> {area_hectares:.2f} ha")
            return area_hectares
        except Exception as e:
            logger.error(f"Error al calcular área del lote: {str(e)}")
            return None

    def calculate_lot_centroid(self, geometry, crs):
        """
        Calcula el centroide de un lote en coordenadas geográficas.
        
        Args:
            geometry: Geometría del lote
            crs: Sistema de coordenadas de referencia del lote
            
        Returns:
            Tupla (x, y) con las coordenadas del centroide o None si hay error
        """
        try:
            if geometry is None or geometry.isEmpty():
                return None
                
            # Obtener el centroide de la geometría
            centroid = geometry.centroid()
            
            if centroid.isEmpty():
                return None
                
            # Obtener las coordenadas del centroide
            centroid_point = centroid.asPoint()
            
            # Si el CRS no es geográfico, transformar a WGS84
            if not crs.isGeographic():
                # Crear transformación al CRS geográfico (WGS84)
                wgs84 = QgsCoordinateReferenceSystem('EPSG:4326')
                transform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
                
                # Transformar el punto
                transformed_point = transform.transform(centroid_point)
                return (transformed_point.x(), transformed_point.y())
            else:
                # Ya está en coordenadas geográficas
                return (centroid_point.x(), centroid_point.y())
                
        except Exception as e:
            logger.error(f"Error al calcular centroide del lote: {str(e)}")
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

    def upload_files_with_progress(self, files, data, progress_callback=None, headers=None):
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
                
                def _calculate_total_size(self):
                    """Calcula el tamaño total de todos los archivos"""
                    total = 0
                    for file_data in self.files.values():
                        if isinstance(file_data, tuple) and len(file_data) > 1:
                            total += len(file_data[1])
                    return total
                
                def _progress_callback(self, chunk):
                    """Callback para cada chunk enviado"""
                    self.uploaded_size += len(chunk)
                    if self.total_size > 0 and self.progress_callback:
                        upload_percent = min(30 + int((self.uploaded_size / self.total_size) * 25), 55)
                        mb_uploaded = self.uploaded_size / (1024*1024)
                        mb_total = self.total_size / (1024*1024)
                        message = f"Subiendo archivos... {mb_uploaded:.1f} MB de {mb_total:.1f} MB ({upload_percent}%)"
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
                                if headers:
                                    auth_headers.update(headers)
                                
                                response = session.post(
                                    DETECT_PALMS_ENDPOINT,
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

    def upload_files_with_real_streaming(self, files, data, progress_callback=None, headers=None):
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
                    'lot_id': data['lot_id'],
                    'orthoimage': ('image.tif', files['orthoimage'][1], 'image/tiff'),
                    'shapefile': ('lots.shp', files['shapefile'][1], 'application/octet-stream'),
                    'dbf_file': ('lots.dbf', files['dbf_file'][1], 'application/octet-stream'),
                    'shx_file': ('lots.shx', files['shx_file'][1], 'application/octet-stream'),
                    'prj_file': ('lots.prj', files['prj_file'][1], 'application/octet-stream')
                }
            )
            
            # Crear el monitor para el progreso
            def progress_monitor(monitor):
                if progress_callback:
                    # Calcular porcentaje basado en bytes enviados
                    total_size = encoder.len
                    uploaded_size = monitor.bytes_read
                    if total_size > 0:
                        upload_percent = min(30 + int((uploaded_size / total_size) * 25), 55)
                        mb_uploaded = uploaded_size / (1024*1024)
                        mb_total = total_size / (1024*1024)
                        message = f"Subiendo archivos... {mb_uploaded:.1f} MB de {mb_total:.1f} MB ({upload_percent}%)"
                        progress_callback(upload_percent, message)
                        QApplication.processEvents()
            
            # Crear el monitor
            monitor = MultipartEncoderMonitor(encoder, progress_monitor)
            
            # Configurar headers con X-API-Key
            auth_headers = {
                'X-API-Key': API_KEY,
                'Content-Type': monitor.content_type,
                'Content-Length': str(monitor.len)
            }
            if headers:
                auth_headers.update(headers)
            logger.info(f"Autenticación X-API-Key configurada para la petición con streaming")
            
            # Realizar la petición con streaming
            session = requests.Session()
            session.timeout = 300  # 5 minutos
            
            response = session.post(
                DETECT_PALMS_ENDPOINT,
                data=monitor,
                headers=auth_headers,
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

    def _clip_with_gdal_warp(self, raster_path, shapefile_path, progress_callback=None):
        """
        Recorta + remuestrea + comprime en una sola pasada con la API de GDAL.

        Es el camino principal porque no depende de QGIS Processing: los algoritmos
        'gdal:cliprasterbymasklayer'/'cliprasterbyextent' fallan de forma intermitente
        según la sesión de QGIS, y al caer al respaldo se subía el recorte sin comprimir
        (más de 32MB = error 413 del backend). GDAL siempre está disponible dentro de QGIS.

        Devuelve la ruta del TIFF recortado, o None si no se pudo (el llamador usa
        entonces los métodos basados en Processing).
        """
        try:
            from osgeo import gdal
        except ImportError as e:
            logger.warning(f"GDAL no disponible para el clip directo: {e}")
            return None
        try:
            if progress_callback:
                progress_callback(15, "Recortando imagen al área del lote...")
                QApplication.processEvents()

            temp_dir = tempfile.mkdtemp(prefix="raster_clip_gdalwarp_")
            out_path = os.path.join(temp_dir, "clipped_image.tif")

            warp_options = gdal.WarpOptions(
                cutlineDSName=shapefile_path,
                cropToCutline=True,
                xRes=0.1,               # 10 cm/píxel (misma resolución de siempre)
                yRes=0.1,
                resampleAlg="bilinear",
                creationOptions=["COMPRESS=LZW", "TILED=YES"],
                multithread=True,
            )
            ds = gdal.Warp(out_path, raster_path, options=warp_options)
            if ds is None:
                logger.warning("gdal.Warp devolvió None en el clip directo")
                return None
            width, height = ds.RasterXSize, ds.RasterYSize
            ds = None

            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                size_mb = os.path.getsize(out_path) / (1024 * 1024)
                logger.info(f"Clip directo con GDAL: {width}x{height} px, {size_mb:.2f} MB (10cm/px, LZW)")
                return out_path
            logger.warning("El clip directo con GDAL no generó un archivo válido")
        except Exception as e:
            logger.warning(f"Clip directo con GDAL falló ({e}); se intenta con QGIS Processing")
        return None

    def clip_raster_with_shapefile(self, raster_path, shapefile_path, progress_callback=None):
        """
        Recorta el raster usando el shapefile como máscara.
        Optimización: Establece la resolución a 10 centímetros por píxel para reducir
        significativamente el tamaño del archivo y mejorar el tiempo de transferencia a la API.
        Retorna la ruta del raster recortado.
        """
        try:
            logger.info(f"Iniciando clip del raster: {raster_path}")
            logger.info(f"Usando shapefile como máscara: {shapefile_path}")

            # Camino principal: GDAL directo (ver _clip_with_gdal_warp). Solo si falla
            # se usan los algoritmos de QGIS Processing de más abajo.
            direct = self._clip_with_gdal_warp(raster_path, shapefile_path, progress_callback)
            if direct:
                return direct

            if progress_callback:
                progress_callback(15, "Preparando clip del raster...")
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
                progress_callback(18, "Ejecutando clip del raster...")
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
            
            # Parámetros para el algoritmo con resolución optimizada a 10cm/pixel
            params = {
                'INPUT': raster_path,
                'MASK': shapefile_path,
                'SOURCE_CRS': None,  # Usar CRS del raster
                'TARGET_CRS': None,  # Mantener CRS original
                'NODATA': None,      # No especificar nodata
                'ALPHA_BAND': False, # No usar banda alfa
                'CROP_TO_CUTLINE': True,  # Recortar al límite del shapefile
                'KEEP_RESOLUTION': False,  # Cambiar resolución para optimizar
                'SET_RESOLUTION': True,  # Establecer resolución específica
                'X_RESOLUTION': 0.1,  # 10 centímetros por píxel
                'Y_RESOLUTION': 0.1,  # 10 centímetros por píxel
                'MULTITHREADING': False,  # No usar multithreading para evitar problemas
                # Compresión sin pérdida (no reduce resolución ni calidad, solo el peso
                # del archivo): sin esto, lotes de más de ~8-10 ha superan los ~32MB que
                # acepta el backend en un solo request y el envío falla con 413.
                'OPTIONS': 'COMPRESS=LZW',
                'DATA_TYPE': 0,  # Tipo de datos automático
                'EXTRA': '',
                'OUTPUT': clipped_raster_path
            }
            
            logger.info("Ejecutando algoritmo de clip con resolución optimizada a 10cm/pixel...")
            
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
            
            # Log del beneficio de la optimización
            try:
                size_bytes = os.path.getsize(clipped_path)
                size_mb = size_bytes / (1024 * 1024)
                logger.info(f"Archivo optimizado generado: {size_mb:.1f} MB (resolución: 10cm/pixel)")
            except Exception as e:
                logger.warning(f"No se pudo obtener el tamaño del archivo optimizado: {str(e)}")
            
            if progress_callback:
                try:
                    size_bytes = os.path.getsize(clipped_path)
                    size_mb = size_bytes / (1024 * 1024)
                    message = (
                        f"Subiendo imagen optimizada al servidor... Tamaño: {size_mb:.1f} MB "
                        f"(resolución: 10cm/pixel). Esta operación será más rápida..."
                    )
                except Exception:
                    message = "Subiendo imagen optimizada al servidor, por favor espere..."
                progress_callback(50, message)
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

    def create_temp_lote_shapefile(self, shapefile_path, lot_id, progress_callback=None, target_crs=None):
        """
        Crea un shapefile temporal con solo el lote seleccionado.
        Si `target_crs` se provee y es válido, la geometría se reproyecta a ese CRS
        para optimizar operaciones posteriores (por ejemplo, clip con el raster).
        """
        try:
            logger.info(f"Creando shapefile temporal para lote {lot_id}")
            
            if progress_callback:
                progress_callback(5, "Extrayendo lote seleccionado...")
                QApplication.processEvents()
            
            # Crear directorio temporal para el shapefile del lote
            temp_dir = tempfile.mkdtemp(prefix="lote_shapefile_")
            temp_shapefile_path = os.path.join(temp_dir, f"lote_{lot_id}.shp")
            
            # Cargar el shapefile completo
            vector_layer = QgsVectorLayer(shapefile_path, "vector", "ogr")
            if not vector_layer.isValid():
                raise Exception(f"No se pudo cargar el shapefile: {shapefile_path}")
            
            logger.info(f"Shapefile cargado. Total de lotes: {vector_layer.featureCount()}")
            
            # Buscar el lote específico por FID
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
                    logger.info(f"Lote FID {lot_id} encontrado")
                    lot_found = True
                    from qgis.core import QgsFeature, QgsVectorFileWriter
                    # Determinar CRS de salida
                    output_crs = target_crs if target_crs and target_crs.isValid() else vector_layer.crs()
                    # Crear una capa temporal con la misma estructura
                    temp_layer = QgsVectorLayer(f"Polygon?crs={output_crs.authid()}", "temp", "memory")
                    temp_layer.dataProvider().addAttributes(vector_layer.fields())
                    temp_layer.updateFields()
                    logger.info(f"Capa temporal creada con {len(vector_layer.fields())} campos")
                    # Usar la geometría completa (incluye multiparte) y medir su área total
                    geom = feature.geometry()
                    geom_full = geom if geom is not None else None
                    # Medir área en CRS original (suma todas las partes si es multiparte)
                    area_hectares = self.calculate_lot_area_hectares(geom_full, vector_layer.crs())
                    logger.info(f"Área del lote medida (geodésica): {area_hectares if area_hectares is not None else 'N/A'} ha")

                    # Reproyectar a CRS de salida si es distinto
                    geom_to_save = geom_full
                    if output_crs != vector_layer.crs():
                        try:
                            transform = QgsCoordinateTransform(vector_layer.crs(), output_crs, QgsProject.instance())
                            geom_to_save = geom_full.clone()
                            geom_to_save.transform(transform)
                            logger.info(f"Lote reproyectado a {output_crs.authid()} para el clip del raster")
                        except Exception as tr_err:
                            logger.warning(f"Fallo al reproyectar lote a {output_crs.authid()}: {str(tr_err)}. Se guardará en CRS original")
                            output_crs = vector_layer.crs()
                            temp_layer = QgsVectorLayer(f"Polygon?crs={output_crs.authid()}", "temp", "memory")
                            temp_layer.dataProvider().addAttributes(vector_layer.fields())
                            temp_layer.updateFields()
                            logger.info(f"Capa temporal recreada con {len(vector_layer.fields())} campos (reproyección)")
                            geom_to_save = geom_full

                    temp_feature = QgsFeature(temp_layer.fields())
                    temp_feature.setGeometry(geom_to_save)
                    for field in vector_layer.fields():
                        temp_feature[field.name()] = feature[field.name()]
                    
                    # El FID se preserva automáticamente en el shapefile temporal
                    # No necesitamos campos adicionales
                    
                    temp_layer.dataProvider().addFeatures([temp_feature])
                    
                    # Sin límite de área: los lotes grandes se dividen automáticamente
                    # en process_detection_smart (ver MAX_SUBAREA_HA), no se bloquean acá.
                    if area_hectares is not None:
                        logger.info(f"Área del lote: {area_hectares:.2f} hectáreas")
                    else:
                        logger.warning("No se pudo calcular el área en hectáreas")
                    
                    # Guardar como shapefile temporal
                    QgsVectorFileWriter.writeAsVectorFormat(
                        temp_layer,
                        temp_shapefile_path,
                        "utf-8",
                        output_crs,
                        "ESRI Shapefile"
                    )
                    logger.info(f"Shapefile temporal creado: {temp_shapefile_path}")
                    break
            
            if not lot_found:
                raise Exception(f"No se encontró el lote {lot_id} en el shapefile")
            
            if progress_callback:
                progress_callback(10, "Subiendo imagen al servidor...")
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
                progress_callback(8, "Usando método alternativo de clip del raster...")
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
            
            # Parámetros para recorte por extensión con resolución optimizada a 10cm/pixel.
            # OPTIONS son las opciones de creación de GDAL (compresión); la resolución
            # (-tr) va en EXTRA porque son argumentos de línea de comandos para
            # gdal_translate, no opciones de creación — estaban intercambiados antes,
            # así que este método alternativo ni remuestreaba ni comprimía realmente.
            params = {
                'INPUT': raster_path,
                'PROJWIN': f"{xmin},{xmax},{ymin},{ymax}",
                'NODATA': None,
                'OPTIONS': 'COMPRESS=LZW',
                'DATA_TYPE': 0,
                'EXTRA': '-tr 0.1 0.1',
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
                progress_callback(22, "Clip del raster completado (método alternativo)")
                QApplication.processEvents()
            
            return clipped_path
                
        except Exception as e:
            logger.error(f"Error en método alternativo de clip: {str(e)}")
            raise Exception(f"Error en método alternativo de clip: {str(e)}")

    # Tamaño máximo de un solo envío al servidor. No es un límite de negocio, es
    # puramente técnico: a 10cm/px con
    # compresión LZW, ~10 ha comprimen a ~9MB; 15 ha deja margen de sobra bajo los
    # ~32MB que acepta Cloud Run por request, incluso si una imagen en particular
    # comprime peor que el promedio (ortofotos muy texturizadas). Lotes más grandes
    # se dividen automáticamente en piezas de este tamaño (ver process_detection_smart)
    # y se fusionan al final — el usuario no nota la diferencia, solo tarda más.
    # 5 ha por bloque: medido sobre ortofoto real ~2.9 MB/ha ya comprimido (LZW apenas
    # reduce fotografía aérea), así que un bloque pesa ~15MB, con buen margen bajo los
    # ~32MB que acepta el backend. Además coincide con la unidad de cobro: 1 bloque = 1 crédito.
    MAX_SUBAREA_HA = 5.0

    # Bloques que se envían al servidor a la vez. El cuello de botella es el tiempo
    # de procesamiento en el servidor (3-5 min por bloque), no la subida, así que
    # mandarlos en paralelo reduce mucho el total. 3 es conservador: Cloud Run
    # levanta más instancias si hace falta, pero cada una carga el modelo al arrancar.
    BLOQUES_EN_PARALELO = 3

    def _split_geometry_recursive(self, geometry, max_area_m2, depth=0):
        """
        Divide `geometry` en pedazos cuya área no supere `max_area_m2`, cortando
        siempre por la mitad del lado más largo del cuadro delimitador y
        recortando cada mitad a la forma real del lote (no rectángulos ciegos).
        Recursivo: cada mitad que siga siendo grande se vuelve a partir.
        """
        from qgis.core import QgsRectangle, QgsGeometry

        area = geometry.area()
        if area <= max_area_m2 or depth > 8:
            return [geometry] if area > 1.0 else []

        bbox = geometry.boundingBox()
        if bbox.width() >= bbox.height():
            mid = bbox.xMinimum() + bbox.width() / 2.0
            rects = [
                QgsRectangle(bbox.xMinimum(), bbox.yMinimum(), mid, bbox.yMaximum()),
                QgsRectangle(mid, bbox.yMinimum(), bbox.xMaximum(), bbox.yMaximum()),
            ]
        else:
            mid = bbox.yMinimum() + bbox.height() / 2.0
            rects = [
                QgsRectangle(bbox.xMinimum(), bbox.yMinimum(), bbox.xMaximum(), mid),
                QgsRectangle(bbox.xMinimum(), mid, bbox.xMaximum(), bbox.yMaximum()),
            ]

        pieces = []
        for rect in rects:
            clipped = geometry.intersection(QgsGeometry.fromRect(rect))
            if clipped and not clipped.isEmpty() and clipped.area() > 1.0:
                pieces.extend(self._split_geometry_recursive(clipped, max_area_m2, depth + 1))
        return pieces

    def _write_single_feature_shapefile(self, geometry, crs, out_path):
        """Guarda una geometría suelta como shapefile de un solo feature (para un sub-lote)."""
        from qgis.core import QgsVectorLayer, QgsFeature, QgsVectorFileWriter

        layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", "sublote", "memory")
        provider = layer.dataProvider()
        layer.updateFields()

        feat = QgsFeature(layer.fields())
        feat.setGeometry(geometry)
        provider.addFeatures([feat])
        layer.updateExtents()

        QgsVectorFileWriter.writeAsVectorFormat(layer, out_path, "utf-8", crs, "ESRI Shapefile")

    def _merge_point_shapefiles(self, shapefile_paths, out_path, crs):
        """Junta los puntos de varios shapefiles de resultado en uno solo, renumerando 'id'."""
        from qgis.core import QgsVectorLayer, QgsFeature, QgsVectorFileWriter, QgsField, QgsGeometry
        from qgis.PyQt.QtCore import QVariant

        merged_layer = QgsVectorLayer(f"Point?crs={crs.authid()}", "palmas_merged", "memory")
        provider = merged_layer.dataProvider()
        provider.addAttributes([QgsField("id", QVariant.Int), QgsField("confianza", QVariant.Double)])
        merged_layer.updateFields()

        next_id = 1
        merged_features = []
        for shp_path in shapefile_paths:
            if not shp_path or not os.path.exists(shp_path):
                continue
            src_layer = QgsVectorLayer(shp_path, "sub_result", "ogr")
            if not src_layer.isValid():
                continue
            for src_feat in src_layer.getFeatures():
                new_feat = QgsFeature(merged_layer.fields())
                geom = src_feat.geometry()
                if src_layer.crs() != crs:
                    transform = QgsCoordinateTransform(src_layer.crs(), crs, QgsProject.instance())
                    geom = QgsGeometry(geom)
                    geom.transform(transform)
                new_feat.setGeometry(geom)
                new_feat["id"] = next_id
                try:
                    new_feat["confianza"] = src_feat["confianza"]
                except Exception:
                    new_feat["confianza"] = None
                merged_features.append(new_feat)
                next_id += 1

        provider.addFeatures(merged_features)
        merged_layer.updateExtents()
        QgsVectorFileWriter.writeAsVectorFormat(merged_layer, out_path, "utf-8", crs, "ESRI Shapefile")
        return len(merged_features)

    def _finalize_output_shapefile(self, shapefile_path, lot_name, lot_geometry, crs, output_folder=None):
        """
        Último paso antes de devolverle el resultado al usuario:
        - Agrega el campo 'Lote' (y 'tipo'='palma') a cada punto detectado.
        - Agrega un punto extra en el centro del lote con tipo='lote_label',
          para poder etiquetar el nombre del lote una sola vez en el mapa
          (no repetido en cada palma).
        - Si se indicó una carpeta de resultados, mueve el shapefile final ahí.
        Devuelve la ruta final del shapefile (puede ser la misma o una nueva).
        """
        from qgis.core import QgsVectorLayer, QgsFeature, QgsField, QgsVectorFileWriter
        from qgis.PyQt.QtCore import QVariant

        layer = QgsVectorLayer(shapefile_path, "resultado", "ogr")
        if not layer.isValid():
            logger.warning("No se pudo abrir el resultado para agregarle el nombre del lote; se devuelve tal cual")
            return shapefile_path

        layer.startEditing()
        field_names = [f.name() for f in layer.fields()]
        if "Lote" not in field_names:
            layer.addAttribute(QgsField("Lote", QVariant.String))
        if "tipo" not in field_names:
            layer.addAttribute(QgsField("tipo", QVariant.String))
        layer.updateFields()

        lote_idx = layer.fields().indexOf("Lote")
        tipo_idx = layer.fields().indexOf("tipo")
        for feat in layer.getFeatures():
            layer.changeAttributeValue(feat.id(), lote_idx, lot_name or "")
            layer.changeAttributeValue(feat.id(), tipo_idx, "palma")

        # Punto-etiqueta con el nombre del lote, en el centro del lote real.
        if lot_geometry is not None:
            try:
                centroid = lot_geometry.centroid()
                label_feat = QgsFeature(layer.fields())
                label_feat.setGeometry(centroid)
                label_feat["Lote"] = lot_name or ""
                label_feat["tipo"] = "lote_label"
                if "id" in field_names:
                    label_feat["id"] = 0
                if "confianza" in field_names:
                    label_feat["confianza"] = None
                layer.addFeature(label_feat)
            except Exception as e:
                logger.warning(f"No se pudo agregar el punto-etiqueta del lote: {str(e)}")

        layer.commitChanges()

        final_path = shapefile_path
        carpeta = carpeta_del_lote(output_folder, lot_name or os.path.splitext(os.path.basename(shapefile_path))[0])
        if carpeta:
            safe_name = nombre_seguro(lot_name or os.path.splitext(os.path.basename(shapefile_path))[0])
            candidate = os.path.join(carpeta, f"{safe_name}_palmas.shp")
            # Evitar pisar un resultado anterior con el mismo nombre de lote.
            counter = 1
            while os.path.exists(candidate):
                candidate = os.path.join(carpeta, f"{safe_name}_palmas_{counter}.shp")
                counter += 1
            try:
                QgsVectorFileWriter.writeAsVectorFormat(layer, candidate, "utf-8", crs, "ESRI Shapefile")
                final_path = candidate
            except Exception as e:
                logger.warning(f"No se pudo guardar en la carpeta de resultados elegida: {str(e)}. Se usa la ruta original.")

        return final_path

    def process_detection_smart(self, image_path, lotes_path, lot_id, progress_callback=None,
                                 lot_name=None, output_folder=None, reanudar=False):
        """
        Punto de entrada recomendado para detectar un lote de cualquier tamaño.
        Si el lote entra dentro de un solo envío seguro (<= MAX_SUBAREA_HA, ver
        esa constante), delega directamente en `process_detection` sin cambiar
        nada del comportamiento de siempre. Si el lote es más grande, lo divide
        en sub-lotes automáticamente (cada uno <= MAX_SUBAREA_HA), corre la
        detección de cada sub-lote con el mismo flujo de subida/espera/descarga
        de siempre, y fusiona todos los resultados en una sola capa de puntos —
        el usuario no tiene que hacer nada distinto a detectar un lote chico.
        """
        from qgis.core import QgsGeometry

        # Estado del procesamiento por bloques: permite informar hasta dónde se
        # llegó cuando un sub-lote falla o se acaban los créditos a mitad de camino.
        self.sublotes_totales = 0
        self.sublotes_completados = 0
        self.sublotes_fallidos = []
        self.detencion_por_creditos = False

        if progress_callback:
            self.progress_callback = progress_callback

        # 1. Leer la geometría real del lote sin pasar por create_temp_lote_shapefile
        #    (esa función corta en seco a los 35 ha; acá decidimos nosotros qué hacer).
        source_layer = QgsVectorLayer(lotes_path, "lotes_origen", "ogr")
        if not source_layer.isValid():
            raise Exception(f"No se pudo cargar el shapefile de lotes: {lotes_path}")

        target_feature = None
        for feature in source_layer.getFeatures():
            if str(feature.id()) == str(lot_id):
                target_feature = feature
                break
        if target_feature is None:
            raise Exception(f"No se encontró el lote {lot_id} en el shapefile")

        # CRS objetivo: el del raster, igual que hace process_detection normalmente.
        raster_layer_for_crs = QgsRasterLayer(image_path, "raster_for_crs")
        target_crs = raster_layer_for_crs.crs() if raster_layer_for_crs.isValid() else source_layer.crs()
        if not target_crs or not target_crs.isValid():
            target_crs = source_layer.crs()

        geom = target_feature.geometry()
        if source_layer.crs() != target_crs:
            transform = QgsCoordinateTransform(source_layer.crs(), target_crs, QgsProject.instance())
            geom = QgsGeometry(geom)
            geom.transform(transform)

        area_ha = self.calculate_lot_area_hectares(geom, target_crs) or (geom.area() / 10000.0)
        logger.info(f"process_detection_smart: área del lote {lot_id} = {area_ha:.2f} ha")

        # Identificador del lote para el cobro: se comparte entre todos sus bloques,
        # así el servidor cobra por área total y no por cantidad de bloques. Al
        # reanudar se reutiliza el mismo (ver más abajo) para no volver a cobrar.
        self.lote_grupo_id = f"{lot_id}-{uuid4().hex[:10]}"

        # 2. Lote de tamaño normal: mismo flujo de siempre, solo se le agrega
        #    el nombre del lote y el punto-etiqueta al final.
        if area_ha <= self.MAX_SUBAREA_HA + 1e-6:
            raw_output = self.process_detection(image_path, lotes_path, lot_id)
            return self._finalize_output_shapefile(raw_output, lot_name, geom, target_crs, output_folder)

        # 3. Lote grande: dividir, procesar cada pedazo, fusionar.
        estado_previo = leer_estado_pendiente(lotes_path, lot_id) if reanudar else None
        resultados_previos = []
        if estado_previo:
            # Reanudar: solo se procesan los bloques que quedaron pendientes y se
            # reutilizan los resultados ya obtenidos (no se vuelve a gastar créditos).
            pieces = [QgsGeometry.fromWkt(w) for w in estado_previo["pendientes_wkt"]]
            pieces = [p for p in pieces if p and not p.isEmpty()]
            resultados_previos = list(estado_previo.get("resultados", []))
            # Nombre de la capa incompleta de la corrida anterior: al terminar el
            # lote se ofrece quitarla, porque la nueva ya incluye todo.
            self.capa_parcial_previa = estado_previo.get("capa_parcial", "")
            if estado_previo.get("lote_grupo_id"):
                # Mismo grupo de cobro que la corrida anterior: el servidor ya tiene
                # registradas las hectáreas pagadas y solo cobrará las que faltan.
                self.lote_grupo_id = estado_previo["lote_grupo_id"]
            logger.info(
                f"Reanudando lote {lot_id}: {len(pieces)} bloques pendientes, "
                f"{len(resultados_previos)} bloques ya procesados antes"
            )
        else:
            logger.info(f"Lote grande ({area_ha:.2f} ha) — dividiendo en sub-lotes de hasta {self.MAX_SUBAREA_HA} ha")
            max_area_m2 = self.MAX_SUBAREA_HA * 10000.0
            pieces = self._split_geometry_recursive(geom, max_area_m2)
        if not pieces:
            raise Exception("No se pudo dividir el lote grande en sub-lotes procesables")

        total = len(pieces)
        self.sublotes_totales = total
        logger.info(f"Lote {lot_id} dividido en {total} sub-lotes")

        original_callback = self.progress_callback
        temp_dir = tempfile.mkdtemp(prefix="lote_grande_")
        result_paths = list(resultados_previos)
        pendientes = []  # bloques que quedaron sin procesar (para poder reanudar)

        try:
            from concurrent.futures import ThreadPoolExecutor, CancelledError

            hilos = min(self.BLOQUES_EN_PARALELO, total)
            logger.info(f"Procesando {total} bloques, {hilos} en paralelo")
            if original_callback:
                original_callback(3, f"Procesando {total} bloques ({hilos} a la vez)...")

            with ThreadPoolExecutor(max_workers=hilos) as ejecutor:
                futuros = {
                    ejecutor.submit(self._procesar_bloque, i, g, image_path, temp_dir, target_crs): (i, g)
                    for i, g in enumerate(pieces, start=1)
                }
                listos = 0
                # No se usa futuro.result() en bucle directo: eso bloquea el hilo
                # principal hasta que el bloque termine y la ventana de progreso se
                # ve congelada. Se revisa cuáles terminaron y entre revisión y
                # revisión se le da aire a la interfaz.
                por_revisar = dict(futuros)
                while por_revisar:
                    terminados = [f for f in por_revisar if f.done()]
                    if not terminados:
                        _procesar_eventos_si_hilo_principal()
                        time.sleep(0.15)
                        continue
                    for futuro in terminados:
                        i, piece_geom = por_revisar.pop(futuro)
                        try:
                            sub_result_path = futuro.result()
                        except CancelledError:
                            pendientes.append(piece_geom)
                            continue
                        except Exception as e:
                            mensaje = str(e)
                            self.sublotes_fallidos.append({"sublote": i, "error": mensaje})
                            logger.error(f"Bloque {i}/{total} falló: {mensaje}")
                            pendientes.append(piece_geom)
                            if "insuficiente" in mensaje.lower():
                                # Sin créditos: cancelar los bloques que aún no arrancaron.
                                self.detencion_por_creditos = True
                                logger.info("Se detiene el procesamiento por falta de créditos")
                                for otro in futuros:
                                    otro.cancel()
                            continue

                        if sub_result_path and os.path.exists(sub_result_path):
                            result_paths.append(sub_result_path)
                            self.sublotes_completados += 1
                            listos += 1
                            if original_callback:
                                original_callback(int(listos / total * 95),
                                                  f"Bloque {listos}/{total} procesado")
                        else:
                            pendientes.append(piece_geom)
                    _procesar_eventos_si_hilo_principal()

            self.progress_callback = original_callback

            # Guardar (o limpiar) el avance para poder reanudar después sin reprocesar
            # ni volver a gastar créditos en los bloques ya resueltos.
            if pendientes:
                result_paths = self._guardar_avance_pendiente(
                    lotes_path, lot_id, image_path, target_crs, result_paths, pendientes)
                self.bloques_pendientes = len(pendientes)
            else:
                # Ojo: la limpieza va DESPUÉS de fusionar. Los shapefiles de bloques
                # ya procesados en corridas anteriores viven en esa misma carpeta y
                # todavía se necesitan para la fusión final.
                self.bloques_pendientes = 0

            if not result_paths:
                detalle = self.sublotes_fallidos[0]["error"] if self.sublotes_fallidos else "sin detalle"
                raise Exception(f"Ningún sub-lote se pudo procesar. Primer error: {detalle}")
            if self.progress_callback:
                self.progress_callback(97, "Uniendo resultados de todos los sub-lotes...")

            # Nombre único por corrida: si se reutiliza el mismo nombre y la capa
            # anterior sigue cargada en QGIS, el archivo queda bloqueado y la fusión
            # produce un shapefile inválido.
            marca = datetime.now().strftime("%Y%m%d%H%M%S")
            final_output = os.path.join(
                os.path.dirname(image_path) or tempfile.gettempdir(),
                f"palmas_lote_{lot_id}_completo_{marca}.shp"
            )
            total_palmas = self._merge_point_shapefiles(result_paths, final_output, target_crs)
            logger.info(f"Lote grande {lot_id}: {total_palmas} palmas detectadas en total, fusionadas en {final_output}")

            final_output = self._finalize_output_shapefile(final_output, lot_name, geom, target_crs, output_folder)
            self.last_output_shapefile = final_output

            # Lote terminado: ya se puede descartar el avance guardado y sus parciales.
            if not pendientes:
                borrar_estado_pendiente(lotes_path, lot_id)
            return final_output

        finally:
            self.progress_callback = original_callback
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                logging.getLogger(__name__).debug(
                    "Fallo no crítico; se continúa.", exc_info=True)

    def _procesar_bloque(self, indice, piece_geom, image_path, temp_dir, target_crs):
        """
        Procesa un bloque completo (recorte + envío + espera + descarga) y devuelve
        la ruta de su shapefile de resultados, ya copiado a `temp_dir`.

        Corre en un hilo del pool: usa su propia instancia de LocalDetector para no
        pisar el estado de las demás, y sin progress_callback para no tocar la
        interfaz desde un hilo secundario.
        """
        sub_shp_path = os.path.join(temp_dir, f"sublote_{indice}.shp")
        with _PREPARACION_LOCK:
            self._write_single_feature_shapefile(piece_geom, target_crs, sub_shp_path)

        detector_bloque = LocalDetector()
        detector_bloque.lote_grupo_id = getattr(self, "lote_grupo_id", "")
        sub_result_path = detector_bloque.process_detection(image_path, sub_shp_path, "0")
        if not sub_result_path or not os.path.exists(sub_result_path):
            return None

        # Copiarlo a nombre propio: cada detector borra su último resultado al
        # arrancar de nuevo, y varios bloques terminan a la vez.
        base = os.path.splitext(sub_result_path)[0]
        copia = os.path.join(temp_dir, f"resultado_sub_{indice}.shp")
        copia_base = os.path.splitext(copia)[0]
        for ext in ['.shp', '.dbf', '.shx', '.prj']:
            if os.path.exists(base + ext):
                shutil.copy2(base + ext, copia_base + ext)
        return copia

    def _guardar_avance_pendiente(self, lotes_path, lot_id, image_path, target_crs,
                                   result_paths, pendientes):
        """
        Guarda los resultados ya obtenidos y la geometría de los bloques que faltan,
        para que una corrida posterior continúe donde quedó.

        Los shapefiles parciales se copian a una carpeta propia del usuario porque
        los temporales de esta corrida se borran al terminar. Devuelve las rutas
        nuevas (persistentes) de esos resultados.
        """
        try:
            destino = os.path.join(PENDIENTES_DIR, f"lote_{lot_id}")
            os.makedirs(destino, exist_ok=True)

            rutas_persistentes = []
            for idx, ruta in enumerate(result_paths, start=1):
                base = os.path.splitext(ruta)[0]
                nuevo_base = os.path.join(destino, f"parcial_{idx}")
                if os.path.abspath(base) == os.path.abspath(nuevo_base):
                    rutas_persistentes.append(nuevo_base + ".shp")
                    continue
                for ext in ['.shp', '.dbf', '.shx', '.prj']:
                    if os.path.exists(base + ext):
                        shutil.copy2(base + ext, nuevo_base + ext)
                rutas_persistentes.append(nuevo_base + ".shp")

            estado = {
                "lot_id": str(lot_id),
                "lotes_path": os.path.abspath(lotes_path),
                "image_path": os.path.abspath(image_path),
                "crs": target_crs.authid() if target_crs else "",
                "resultados": rutas_persistentes,
                "lote_grupo_id": getattr(self, "lote_grupo_id", ""),
                "pendientes_wkt": [g.asWkt() for g in pendientes],
                "fecha": datetime.now().isoformat(timespec="seconds"),
            }
            os.makedirs(PENDIENTES_DIR, exist_ok=True)
            with open(_ruta_estado_pendiente(lotes_path, lot_id), "w", encoding="utf-8") as f:
                json.dump(estado, f, ensure_ascii=False)
            logger.info(f"Avance guardado: {len(rutas_persistentes)} bloques listos, {len(pendientes)} pendientes")
            return rutas_persistentes
        except Exception as e:
            logger.warning(f"No se pudo guardar el avance para reanudar: {e}")
            return result_paths

    def process_detection(self, image_path, lotes_path, lot_id):
        """
        Procesa la detección usando la API del servidor.
        """
        temp_dir = None
        output_shapefile = None
        clipped_raster_path = None
        temp_lote_shapefile = None
        extract_dir = None
        
        try:
            logger.info(f"Iniciando nueva detección")
            logger.info(f"Imagen: {image_path}")
            logger.info(f"Shapefile: {lotes_path}")
            logger.info(f"FID del lote: {lot_id}")
            
            # Limpiar resultados anteriores si existen
            if self.last_output_shapefile and os.path.exists(self.last_output_shapefile):
                logger.info(f"Limpiando resultados anteriores: {self.last_output_shapefile}")
                try:
                    base_path = os.path.splitext(self.last_output_shapefile)[0]
                    files_cleaned = 0
                    for ext in ['.shp', '.dbf', '.shx', '.prj']:
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
            # No necesitamos crear un nombre fijo aquí
            output_shapefile = None
            logger.info("El archivo de salida será determinado por el contenido del ZIP de la API")
            
            # Determinar CRS objetivo a partir del raster para evitar reproyecciones costosas
            try:
                raster_layer_for_crs = QgsRasterLayer(image_path, "raster_for_crs")
                target_crs = raster_layer_for_crs.crs() if raster_layer_for_crs.isValid() else None
                if target_crs and target_crs.isValid():
                    logger.info(f"CRS del raster (objetivo para el lote): {target_crs.authid()}")
                else:
                    logger.warning("No se pudo obtener CRS del raster; se usará CRS del lote original")
            except Exception as crs_err:
                logger.warning(f"No se pudo leer CRS del raster: {str(crs_err)}")
                target_crs = None

            # Crear un shapefile temporal con solo el lote seleccionado (reproyectando al CRS del raster si está disponible)
            logger.info(f"Creando shapefile temporal con solo el lote FID {lot_id}")
            if self.progress_callback:
                self.progress_callback(5, f"Preparando lote {lot_id}...")
            with _PREPARACION_LOCK:
                temp_lote_shapefile, lot_area_hectares = self.create_temp_lote_shapefile(
                    lotes_path, lot_id, self.progress_callback, target_crs=target_crs
                )
            logger.info(f"Shapefile temporal creado: {temp_lote_shapefile}")
            
            # Calcular centroide del lote para el log
            lot_centroid = None
            try:
                # Cargar el shapefile temporal para calcular el centroide
                temp_layer = QgsVectorLayer(temp_lote_shapefile, "temp_lote", "ogr")
                if temp_layer.isValid() and temp_layer.featureCount() > 0:
                    feature = next(temp_layer.getFeatures())
                    lot_centroid = self.calculate_lot_centroid(feature.geometry(), temp_layer.crs())
                    logger.info(f"Centroide del lote calculado: {lot_centroid}")
            except Exception as e:
                logger.warning(f"No se pudo calcular centroide del lote: {str(e)}")
            
            # Asegurar que el área se pase correctamente
            if lot_area_hectares is None:
                # Recalcular área si no se obtuvo
                try:
                    temp_layer = QgsVectorLayer(temp_lote_shapefile, "temp_lote_area", "ogr")
                    if temp_layer.isValid() and temp_layer.featureCount() > 0:
                        feature = next(temp_layer.getFeatures())
                        lot_area_hectares = self.calculate_lot_area_hectares(feature.geometry(), temp_layer.crs())
                        logger.info(f"Área recalculada: {lot_area_hectares} hectáreas")
                except Exception as e:
                    logger.warning(f"No se pudo recalcular área: {str(e)}")
            
            # El área se calculará en la API
            logger.info(f"Área del lote será calculada en la API")
            
            # Mostrar información del área en el progreso si está disponible
            if lot_area_hectares is not None:
                if self.progress_callback:
                    self.progress_callback(12, f"Lote {lot_id} preparado - Área: {lot_area_hectares:.2f} hectáreas")
                    QApplication.processEvents()
            
            # Hacer clip del raster usando solo el lote seleccionado
            logger.info(f"Realizando clip del raster para reducir tamaño")
            logger.info(f"Archivo original: {image_path}")
            logger.info(f"Tamaño del archivo original: {os.path.getsize(image_path) / (1024*1024):.2f} MB")
            logger.info(f"Shapefile del lote seleccionado: {temp_lote_shapefile}")
            
            if self.progress_callback:
                self.progress_callback(12, "Por favor espere, esta operación puede tardar algunos minutos...")
                QApplication.processEvents()
            
            if self.progress_callback:
                self.progress_callback(15, "Recortando imagen al área del lote...")
            
            # Serializado: el recorte usa GDAL/QGIS, que no admite varios hilos a la vez.
            with _PREPARACION_LOCK:
                clipped_raster_path = self.clip_raster_with_shapefile(image_path, temp_lote_shapefile, self.progress_callback)
            
            logger.info(f"Clip completado. Archivo recortado: {clipped_raster_path}")
            if os.path.exists(clipped_raster_path):
                logger.info(f"Tamaño del archivo recortado: {os.path.getsize(clipped_raster_path) / (1024*1024):.2f} MB")
            else:
                logger.error(f"El archivo recortado no existe: {clipped_raster_path}")
                raise Exception("El archivo recortado no se generó correctamente")
            
            # Asegurar 3 bandas (RGB): ortomosaicos de 4+ bandas pueden fallar en detección
            clipped_raster_path = _ensure_rgb_raster(clipped_raster_path, self.progress_callback)

            # Red de seguridad: si el recorte quedó sin comprimir (algún camino de
            # QGIS Processing ignora las opciones de creación), re-comprimir antes de subir.
            clipped_raster_path = _compress_raster_if_needed(
                clipped_raster_path, progress_callback=self.progress_callback)

            # Preparar los archivos para enviar (usar el raster recortado y el shapefile del lote)
            logger.info(f"Preparando archivos para enviar")
            logger.info(f"Archivo raster a enviar: {clipped_raster_path}")
            clipped_size_bytes = os.path.getsize(clipped_raster_path)
            clipped_size_mb = clipped_size_bytes / (1024 * 1024)
            # Suponiendo 10 Mbps de subida (1.25 MB/s)
            estimated_seconds = int(clipped_size_mb / 1.25)

            # Mensaje informativo: imagen optimizada y tamaño
            if hasattr(self, 'progress_dialog') and self.progress_dialog:
                self.progress_dialog.set_info_message(f"La imagen se ha optimizado, recortado y pesa {clipped_size_mb:.1f} MB")
                self.progress_dialog.update_progress(0, "Esta operación puede tardar algunos minutos.")
                QApplication.processEvents()
                QApplication.processEvents()  # Procesar eventos adicionales
                time.sleep(0.3)  # Reducir sleep
                QApplication.processEvents()
                QApplication.processEvents()
            
            # Procesar eventos antes de leer archivos grandes (puede ser bloqueante).
            # Solo desde el hilo principal: al procesar bloques en paralelo esto corre
            # en hilos secundarios, donde tocar la interfaz de Qt rompe QGIS.
            _procesar_eventos_si_hilo_principal()
            
            with open(clipped_raster_path, 'rb') as img_file, \
                 open(temp_lote_shapefile, 'rb') as shp_file, \
                 open(temp_lote_shapefile.replace('.shp', '.dbf'), 'rb') as dbf_file, \
                 open(temp_lote_shapefile.replace('.shp', '.shx'), 'rb') as shx_file, \
                 open(temp_lote_shapefile.replace('.shp', '.prj'), 'rb') as prj_file:
                
                files = {
                    'orthoimage': ('image.tif', img_file.read()),
                    'shapefile': ('lots.shp', shp_file.read()),
                    'dbf_file': ('lots.dbf', dbf_file.read()),
                    'shx_file': ('lots.shx', shx_file.read()),
                    'prj_file': ('lots.prj', prj_file.read())
                }
            
            # Generar request_id y request_info para historial en API
            request_id = uuid4().hex
            try:
                project_path = QgsProject.instance().fileName()
            except Exception:
                project_path = ""
            # Obtener versión de QGIS de manera segura
            try:
                qgis_version = Qgis.QGIS_VERSION
            except Exception:
                try:
                    qgis_version = QgsApplication.version()
                except Exception:
                    qgis_version = "unknown"

            request_info = {
                'request_id': request_id,
                'client_app': 'QGIS-DetectorPalmas',
                'plugin_version': PLUGIN_NAME,
                'os': platform.platform(),
                'python_version': sys.version.split(" ")[0],
                'qgis_version': qgis_version,
                'project_path': project_path,
                'timestamp': datetime.now().isoformat()
            }

            # Enviar el FID normalizado a la API
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
                
            # Obtener documento del usuario si está autenticado
            user_document = None
            if hasattr(self, 'dockwidget') and self.dockwidget and hasattr(self.dockwidget, 'auth_credentials'):
                user_document = self.dockwidget.auth_credentials.get('document')
                logger.info(f"[{request_id}] Documento del usuario obtenido: {user_document}")
            else:
                # Fallback: intentar obtener desde el dockwidget directamente
                if hasattr(self, 'dockwidget') and self.dockwidget:
                    if hasattr(self.dockwidget, 'user_document'):
                        user_document = self.dockwidget.user_document
                    elif hasattr(self.dockwidget, 'document'):
                        user_document = self.dockwidget.document
                logger.warning(f"[{request_id}] No se pudo obtener documento del usuario")
            
            from ..client_identity import get_client_id
            data = {
                'lot_id': lot_id_normalized,
                'request_info': json.dumps(request_info),
                'user_document': user_document,
                'lot_centroid': json.dumps(lot_centroid) if lot_centroid is not None else '',
                'lot_area': str(lot_area_hectares) if lot_area_hectares is not None else '',
                'client_id': get_client_id(),
                # Identifica todos los bloques de un mismo lote: el servidor suma sus
                # hectáreas y cobra ceil(total/5), en vez de 1 crédito por bloque.
                'lote_grupo_id': getattr(self, 'lote_grupo_id', '') or '',
            }
            
            # LOG TEMPORAL: Debug del diccionario data antes de enviar
            logger.info(f"=== DEBUG DICCIONARIO DATA ===")
            logger.info(f"data['lot_centroid']: {data['lot_centroid']}")
            logger.info(f"data['user_document']: {data['user_document']}")
            logger.info(f"=== FIN DEBUG DICCIONARIO DATA ===")
            headers = {'X-Request-ID': request_id}
            logger.info(f"[{request_id}] Valor de FID enviado a la API: {lot_id_normalized}")
            logger.info(f"[{request_id}] URL de la API: {self.api_url}")
            logger.info(f"[{request_id}] Datos enviados: {data}")
            
            # Verificar conexión con el servidor antes de enviar
            logger.info(f"[{request_id}] Verificando conexión con servidor...")
            if not self.verify_server_connection():
                raise Exception("No se pudo conectar con el servidor. Verifique que el servidor esté funcionando.")
            logger.info(f"[{request_id}] Conexión con servidor verificada correctamente")
            
            # Verificar si el servidor tiene información sobre lotes disponibles
            logger.info(f"[{request_id}] Verificando disponibilidad del lote FID {lot_id_normalized} en el servidor...")
            try:
                # Intentar obtener información sobre lotes disponibles del servidor
                import requests
                lots_response = requests.get(f"{self.api_url}/lots/", timeout=10)
                if lots_response.status_code == 200:
                    logger.info(f"[{request_id}] Servidor tiene información sobre lotes disponibles")
                    logger.info(f"[{request_id}] Respuesta del servidor: {lots_response.text[:200]}...")
                else:
                    logger.info(f"[{request_id}] Servidor no tiene endpoint de lotes disponibles (código: {lots_response.status_code})")
            except Exception as e:
                logger.info(f"[{request_id}] No se pudo verificar lotes disponibles en el servidor: {str(e)}")
            
            # Iniciar la detección en un hilo separado usando cola async
            def detection_thread():
                nonlocal temp_dir, output_shapefile, request_id, lot_id_normalized
                job_id = None
                try:
                    logger.info(f"[THREAD] Hilo de detección iniciado")
                    logger.info(f"[THREAD] Enviando petición al servidor (modo cola async)")
                    
                    # Calcular tamaño total de archivos
                    total_size = 0
                    for file_data in files.values():
                        if isinstance(file_data, tuple) and len(file_data) > 1:
                            total_size += len(file_data[1])
                    
                    logger.info(f"Tamaño total de archivos a subir: {total_size / (1024*1024):.2f} MB")
                    
                    # Enviar a la cola async
                    auth_headers = _cabeceras_autenticacion()
                    if headers:
                        auth_headers.update(headers)
                    
                    # Actualizar progreso inicial
                    if self.progress_callback:
                        self.progress_callback(0, "Su solicitud ha ingresado a la cola de procesos.")
                    
                    # Hacer la petición POST
                    logger.info(f"[THREAD] Enviando POST a {DETECT_PALMS_ASYNC_ENDPOINT}")
                    logger.info(f"[THREAD] Tamaño de archivos: {total_size / (1024*1024):.2f} MB")
                    
                    # IMPORTANTE: requests.post() puede bloquear el hilo con archivos grandes
                    # Pero está en un hilo separado, así que no debería bloquear QGIS
                    try:
                        logger.info("[THREAD] Iniciando requests.post()...")
                        post_start = time.time()
                        response = requests.post(
                            DETECT_PALMS_ASYNC_ENDPOINT,
                            files=files,
                            data=data,
                            headers=auth_headers,
                            timeout=UPLOAD_TIMEOUT
                        )
                        post_time = time.time() - post_start
                        logger.info(f"[THREAD] requests.post() completado en {post_time:.2f}s. Status: {response.status_code}")
                    except Exception as e:
                        logger.error(f"[THREAD] Error en requests.post: {str(e)}")
                        logger.error(f"[THREAD] Traceback: {traceback.format_exc()}")
                        raise
                    
                    if response.status_code != 200:
                        # Manejo detallado de errores según el código HTTP
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
                            # Analizar el contenido del error para dar información más específica
                            if "No se encontró el lote" in error_content:
                                error_msg = f"Error del servidor: 500 - El servidor no puede encontrar el lote con FID {lot_id_normalized}.\n\nEsto puede indicar que:\n- El servidor tiene su propia base de datos de lotes\n- El FID {lot_id_normalized} no existe en la base de datos del servidor\n- Hay un problema de sincronización entre QGIS y el servidor\n\nIntenta con otro lote o verifica la configuración del servidor."
                            else:
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
                    
                    # Construir mensaje informativo con tamaño de archivos (sin estimar un
                    # tiempo en minutos: la relación tamaño/tiempo real varía demasiado
                    # como para que ese número sea confiable, y termina siendo engañoso).
                    total_size_mb = total_size / (1024 * 1024)

                    info_message = (
                        f"Tamaño total de archivos: {total_size_mb:.1f} MB. "
                        f"Esta operación puede tardar algunos minutos dependiendo del tamaño de la imagen "
                        f"y la cantidad de usuarios utilizando el plugin simultáneamente."
                    )

                    # Establecer mensaje informativo en el diálogo si está disponible
                    if hasattr(self, 'progress_dialog') and self.progress_dialog:
                        if hasattr(self.progress_dialog, 'set_info_message'):
                            self.progress_dialog.set_info_message(
                                f"Imagen: {total_size_mb:.1f} MB.\n"
                                f"Puede tardar unos minutos."
                            )
                    
                    # Actualizar progreso después de subir (sin porcentajes, solo mensaje)
                    self.safe_progress_callback(0, "Su solicitud está siendo procesada. Por favor espere...")
                    
                    logger.info(f"Iniciando polling para job_id: {job_id}")
                    
                    # Polling del estado cada 15 segundos
                    polling_start_time = time.time()
                    last_status = None
                    last_message = None
                    
                    while True:
                        # Verificar timeout
                        if time.time() - polling_start_time > QUEUE_TIMEOUT:
                            raise Exception(f"Timeout: La detección tardó más de {QUEUE_TIMEOUT} segundos")
                        
                        # Consultar estado usando el job_id
                        status_url = f"{STATUS_ENDPOINT}/{job_id}"
                        logger.info(f"[THREAD] Consultando estado en: {status_url} (intento de polling)")
                        
                        try:
                            status_response = requests.get(status_url, headers=auth_headers, timeout=30)
                        except Exception as e:
                            logger.warning(f"Error consultando estado: {str(e)}")
                            # Esperar (NO llamar processEvents desde hilo secundario)
                            time.sleep(QUEUE_POLLING_INTERVAL)
                            continue
                        
                        if status_response.status_code != 200:
                            logger.warning(f"Error consultando estado: {status_response.status_code} - {status_response.text}")
                            # Esperar (NO llamar processEvents desde hilo secundario)
                            time.sleep(QUEUE_POLLING_INTERVAL)
                            continue
                        
                        status_data = status_response.json()
                        current_status = status_data.get('status')
                        current_message = status_data.get('message', 'Procesando...')
                        
                        logger.info(f"Estado recibido: status={current_status}, message={current_message}")
                        
                        # Actualizar progreso solo con mensaje (sin porcentajes)
                        # Solo actualizar si cambió el mensaje o el estado para evitar actualizaciones innecesarias
                        if current_message != last_message or current_status != last_status:
                            # Usar el mensaje del servidor si está disponible, sino usar el mensaje informativo
                            display_message = current_message if current_message else info_message
                            self.safe_progress_callback(0, display_message)
                            last_message = current_message
                            last_status = current_status
                        
                        # Verificar si está completado
                        if current_status == 'completed':
                            # Actualizar mensaje antes de descargar (sin porcentajes)
                            self.safe_progress_callback(0, "Procesamiento completado. Descargando resultados...")
                            logger.info(f"Job {job_id} completado, descargando resultados...")
                            break
                        elif current_status == 'failed':
                            error_msg = status_data.get('error', 'Error desconocido')
                            raise Exception(f"Detección falló: {error_msg}")
                        
                        # Esperar antes de la siguiente consulta
                        # NO llamar processEvents desde hilo secundario - puede bloquear QGIS
                        time.sleep(QUEUE_POLLING_INTERVAL)
                    
                    # Descargar resultado - hacer polling recursivo hasta obtener el resultado
                    # Aunque el status sea 'completed', el endpoint puede retornar 202 si aún está finalizando
                    self.safe_progress_callback(0, "Descargando resultados del servidor...")
                    
                    result_url = f"{RESULT_ENDPOINT}/{job_id}"
                    result_response = None
                    max_result_retries = 60  # Hasta 60 intentos (ej. 15s cada uno = hasta 15 min) cuando la API está cargada
                    result_retry_count = 0
                    
                    while result_retry_count < max_result_retries:
                        try:
                            logger.info(f"[THREAD] Intentando descargar resultado (intento {result_retry_count + 1}/{max_result_retries})")
                            result_response = requests.get(result_url, headers=auth_headers, timeout=300, stream=True)
                            
                            # Si retorna 200, tenemos el resultado
                            if result_response.status_code == 200:
                                logger.info(f"[THREAD] Resultado obtenido exitosamente")
                                break
                            
                            # Si retorna 202, aún está procesando - continuar polling
                            elif result_response.status_code == 202:
                                logger.info(f"[THREAD] Job aún finalizando, esperando {QUEUE_POLLING_INTERVAL} segundos...")
                                self.safe_progress_callback(0, "Finalizando procesamiento, por favor espere...")
                                time.sleep(QUEUE_POLLING_INTERVAL)
                                result_retry_count += 1
                                continue
                            
                            # Si retorna otro código, puede ser un error temporal - reintentar
                            else:
                                logger.warning(f"[THREAD] Código de respuesta inesperado: {result_response.status_code}")
                                if result_retry_count < max_result_retries - 1:
                                    time.sleep(QUEUE_POLLING_INTERVAL)
                                    result_retry_count += 1
                                    continue
                                else:
                                    # Último intento falló
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
                    temp_dir = tempfile.mkdtemp(prefix="palm_detection_")
                    logger.info(f"Directorio temporal creado: {temp_dir}")
                    
                    zip_path = os.path.join(temp_dir, "detected_palms.zip")
                    
                    # Descargar archivo con progreso
                    downloaded_size = 0
                    total_size = int(result_response.headers.get('Content-Length', 0))
                    
                    with open(zip_path, 'wb') as f:
                        for chunk in result_response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded_size += len(chunk)
                                if self.progress_callback and total_size > 0:
                                    # Mostrar mensaje con tamaño descargado (sin porcentajes)
                                    downloaded_mb = downloaded_size / (1024*1024)
                                    total_mb = total_size / (1024*1024)
                                    self.safe_progress_callback(0, f"Descargando resultados... {downloaded_mb:.1f} MB de {total_mb:.1f} MB")
                                    # NO llamar processEvents desde hilo secundario
                    
                    logger.info(f"Archivo ZIP guardado: {zip_path}")
                    
                    # Continuar con el procesamiento del ZIP (código existente)
                    # Descomprimir en un directorio temporal (NO en la carpeta de la imagen)
                    extract_dir = tempfile.mkdtemp(prefix="palm_results_")
                    logger.info(f"Descomprimiendo archivos en directorio temporal: {extract_dir}")
                    
                    # Mostrar progreso de extracción
                    self.safe_progress_callback(95, "Extrayendo archivos de resultados...")
                    
                    # Lista para almacenar los archivos extraídos
                    extracted_files = []
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        # Obtener lista de archivos en el ZIP
                        zip_file_list = zip_ref.namelist()
                        logger.info(f"Archivos en el ZIP: {zip_file_list}")
                        
                        # DEBUG: Verificar qué shapefiles hay en el ZIP
                        shapefiles_in_zip = [f for f in zip_file_list if f.lower().endswith('.shp')]
                        logger.info(f"DEBUG: Shapefiles encontrados en ZIP: {shapefiles_in_zip}")
                        
                        # Extraer todos los archivos en directorio temporal
                        zip_ref.extractall(extract_dir)
                        
                        # Buscar el archivo shapefile de palmas (excluyendo outliers y lotes)
                        shapefile_found = None
                        logger.info(f"DEBUG: Buscando shapefile de palmas en ZIP...")
                        
                        # Prioridad 1: Buscar shapefile que contenga "palmas" y no "outliers"
                        for file_name in zip_file_list:
                            if (file_name.lower().endswith('.shp') and 
                                'palmas' in file_name.lower() and 
                                'outliers' not in file_name.lower()):
                                shapefile_found = os.path.join(extract_dir, file_name)
                                logger.info(f"DEBUG: Shapefile de palmas encontrado: {file_name}")
                                break
                        
                        # Prioridad 2: Buscar cualquier shapefile que no sea outliers ni lote
                        if not shapefile_found:
                            for file_name in zip_file_list:
                                if (file_name.lower().endswith('.shp') and 
                                    'outliers' not in file_name.lower() and
                                    'lote' not in file_name.lower()):
                                    shapefile_found = os.path.join(extract_dir, file_name)
                                    logger.info(f"DEBUG: Shapefile encontrado (sin outliers/lote): {file_name}")
                                    break
                        
                        # Prioridad 3: Buscar cualquier shapefile (fallback)
                        if not shapefile_found:
                            for file_name in zip_file_list:
                                if file_name.lower().endswith('.shp'):
                                    shapefile_found = os.path.join(extract_dir, file_name)
                                    logger.info(f"DEBUG: Shapefile encontrado (fallback): {file_name}")
                                    break
                    
                    # Verificar que se encontró un shapefile
                    if not shapefile_found or not os.path.exists(shapefile_found):
                        raise Exception(f"No se encontró ningún shapefile en el archivo ZIP")
                    
                    logger.info(f"Shapefile extraído: {shapefile_found}")
                    
                    # Mostrar progreso de procesamiento
                    if self.progress_callback:
                        self.progress_callback(98, "Procesando archivos de resultados...")
                        QApplication.processEvents()
                    
                    # Guardar resultados en carpeta única por lote y momento (junto al TIFF; evita sobrescrituras)
                    image_dir = os.path.dirname(image_path)
                    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    folder_name = f"detector_palmas_FID{lot_id_normalized}_{timestamp}"
                    output_dir = os.path.join(image_dir, folder_name)
                    try:
                        os.makedirs(output_dir, exist_ok=True)
                    except Exception as e:
                        logger.warning(f"No se pudo crear carpeta {output_dir}, usando directorio de imagen: {e}")
                        output_dir = image_dir
                    final_shapefile = os.path.join(output_dir, os.path.basename(shapefile_found))
                    
                    # Copiar el archivo principal y sus archivos relacionados
                    base_name = os.path.splitext(shapefile_found)[0]
                    for ext in ['.shp', '.dbf', '.shx', '.prj']:
                        source_file = base_name + ext
                        dest_file = os.path.join(output_dir, os.path.basename(source_file))
                        if os.path.exists(source_file):
                            shutil.copy2(source_file, dest_file)
                            logger.info(f"Archivo copiado: {os.path.basename(dest_file)}")
                    
                    # Usar el archivo final como resultado
                    output_shapefile = final_shapefile
                    logger.info(f"Archivos de resultados procesados: {output_shapefile}")
                    
                    # Limpiar directorio temporal de extracción
                    try:
                        shutil.rmtree(extract_dir)
                        logger.info(f"Directorio temporal de extracción eliminado: {extract_dir}")
                    except Exception as e:
                        logger.warning(f"No se pudo eliminar directorio temporal de extracción: {str(e)}")
                    
                    # Mostrar progreso final
                    self.safe_progress_callback(0, "Procesamiento completado exitosamente")
                    
                    # Guardar la ruta del shapefile actual
                    self.last_output_shapefile = output_shapefile
                    logger.info(f"Detección completada exitosamente")
                    
                    # Actualizar resultado para el hilo principal
                    detection_result['output_shapefile'] = output_shapefile
                    detection_result['completed'] = True
                    
                    return output_shapefile
                except Exception as e:
                    error_msg = str(e)
                    error_traceback = traceback.format_exc()
                    logger.error(f"[THREAD] Error en la detección: {error_msg}")
                    logger.error(f"[THREAD] Traceback completo:\n{error_traceback}")
                    
                    # Construir mensaje de error más informativo
                    if job_id:
                        error_detail = f"Error durante el procesamiento del job {job_id}: {error_msg}"
                    else:
                        error_detail = f"Error durante el procesamiento: {error_msg}"
                    
                    # Si el error contiene información específica, usarla
                    if "Timeout" in error_msg:
                        error_detail = f"Timeout: El proceso tardó demasiado tiempo. {error_msg}"
                    elif "Error descargando resultado" in error_msg:
                        error_detail = f"Error al obtener los resultados del servidor. {error_msg}"
                    elif "Error consultando estado" in error_msg:
                        error_detail = f"Error al consultar el estado del proceso. {error_msg}"
                    
                    self.safe_progress_callback(0, f"Error: {error_detail}")
                    detection_result['error'] = error_detail
                    detection_result['completed'] = True
                    # NO hacer raise aquí - solo marcar como completado con error
            
            # Variables para comunicación entre hilos
            detection_result = {'output_shapefile': None, 'error': None, 'completed': False}
            
            # Función para manejar la finalización del hilo
            def on_detection_complete():
                detection_result['completed'] = True
            
            # Iniciar el hilo de detección
            logger.info(f"Iniciando hilo de detección")
            detection_thread = threading.Thread(target=detection_thread)
            detection_thread.daemon = True  # Hilo daemon para que no bloquee la aplicación
            detection_thread.start()
            
            # Mostrar mensaje inicial inmediatamente
            if self.progress_callback:
                try:
                    self.progress_callback(8, "Iniciando proceso...")
                    QApplication.processEvents()
                except Exception as e:
                    logger.error(f"Error al mostrar mensaje inicial: {str(e)}")
            
            # Esperar a que termine el hilo de detección sin bloquear la interfaz
            # IMPORTANTE: NO actualizar el progreso aquí - el hilo secundario se encarga de eso
            logger.info(f"Esperando a que termine el hilo de detección")
            timeout_seconds = 600  # 10 minutos de timeout
            start_time = time.time()
            
            while not detection_result['completed'] and detection_thread.is_alive():
                # Verificar timeout
                if time.time() - start_time > timeout_seconds:
                    logger.error("Timeout esperando la detección")
                    raise Exception("Timeout: La detección tardó demasiado tiempo")
                
                # Procesar eventos de Qt múltiples veces para mantener la UI más responsiva
                # NO actualizar el progreso aquí - el hilo secundario lo hace
                QApplication.processEvents()
                QApplication.processEvents()  # Procesar eventos adicionales para mejor responsividad
                time.sleep(0.05)  # Reducir sleep para mejor responsividad (50ms en lugar de 100ms)
            
            # Verificar si el hilo terminó correctamente
            if not detection_result['completed']:
                logger.error("El hilo de detección no se completó correctamente")
                raise Exception("El proceso de detección no se completó")
            
            # Verificar si hubo error en el hilo
            if detection_result['error']:
                raise Exception(detection_result['error'])
            
            # Obtener el resultado del hilo
            output_shapefile = detection_result['output_shapefile']
            
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
            
            # Limpiar el archivo temporal del raster recortado
            if clipped_raster_path and os.path.exists(clipped_raster_path):
                try:
                    # Obtener el directorio del archivo recortado
                    clipped_dir = os.path.dirname(clipped_raster_path)
                    if os.path.exists(clipped_dir):
                        shutil.rmtree(clipped_dir)
                        logger.info(f"Directorio del raster recortado eliminado: {clipped_dir}")
                except Exception as e:
                    logger.error(f"Error al eliminar archivo temporal del raster: {str(e)}")
            
            # Limpiar el shapefile temporal del lote
            if temp_lote_shapefile and os.path.exists(temp_lote_shapefile):
                try:
                    # Obtener el directorio del shapefile temporal
                    temp_lote_dir = os.path.dirname(temp_lote_shapefile)
                    if os.path.exists(temp_lote_dir):
                        shutil.rmtree(temp_lote_dir)
                        logger.info(f"Directorio del shapefile temporal del lote eliminado: {temp_lote_dir}")
                except Exception as e:
                    logger.error(f"Error al eliminar shapefile temporal del lote: {str(e)}")
            
            logger.info(f"Proceso completado exitosamente")
            return output_shapefile
                    
        except Exception as e:
            logger.error(f"Error en la detección: {str(e)}")
            raise Exception(f"Error en la detección: {str(e)}")
        
        finally:
            # Limpieza garantizada de archivos temporales
            logger.info("Iniciando limpieza de archivos temporales...")
            
            # Limpiar el directorio temporal principal
            if temp_dir and isinstance(temp_dir, str) and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Directorio temporal eliminado: {temp_dir}")
                except Exception as cleanup_error:
                    logger.error(f"Error al eliminar directorio temporal: {str(cleanup_error)}")
            
            # Limpiar el archivo temporal del raster recortado
            if clipped_raster_path and os.path.exists(clipped_raster_path):
                try:
                    clipped_dir = os.path.dirname(clipped_raster_path)
                    if os.path.exists(clipped_dir):
                        shutil.rmtree(clipped_dir)
                        logger.info(f"Directorio del raster recortado eliminado: {clipped_dir}")
                except Exception as cleanup_error:
                    logger.error(f"Error al eliminar archivo temporal del raster: {str(cleanup_error)}")
            
            # Limpiar el shapefile temporal del lote
            if temp_lote_shapefile and os.path.exists(temp_lote_shapefile):
                try:
                    temp_lote_dir = os.path.dirname(temp_lote_shapefile)
                    if os.path.exists(temp_lote_dir):
                        shutil.rmtree(temp_lote_dir)
                        logger.info(f"Directorio del shapefile temporal del lote eliminado: {temp_lote_dir}")
                except Exception as cleanup_error:
                    logger.error(f"Error al eliminar shapefile temporal del lote: {str(cleanup_error)}")
            
            # Limpiar el directorio temporal de extracción
            if extract_dir and os.path.exists(extract_dir):
                try:
                    shutil.rmtree(extract_dir)
                    logger.info(f"Directorio temporal de extracción eliminado: {extract_dir}")
                except Exception as cleanup_error:
                    logger.error(f"Error al eliminar directorio temporal de extracción: {str(cleanup_error)}")
            
            logger.info("Limpieza de archivos temporales completada")

def cleanup_plugin_cache_and_logs():
    """
    Limpia cache, logs y configuraciones problemáticas al instalar/actualizar el plugin.
    Esto previene problemas con datos antiguos que pueden causar resultados erróneos.
    """
    try:
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        settings = QSettings()
        
        # Obtener versión actual del plugin desde metadata.txt
        metadata_path = os.path.join(plugin_dir, 'metadata.txt')
        current_version = None
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.startswith('version='):
                            current_version = line.split('=', 1)[1].strip()
                            break
            except Exception as e:
                logger.warning(f"No se pudo leer la versión del plugin: {str(e)}")
        
        # Obtener versión guardada anteriormente
        last_version = settings.value('detector_palmas/version', None)
        
        # Si es una nueva versión o primera instalación, limpiar todo
        if current_version and current_version != last_version:
            logger.info(f"Detectada nueva versión del plugin: {current_version} (anterior: {last_version})")
            logger.info("Limpiando cache, logs y configuraciones problemáticas...")
            
            # 1. Limpiar logs antiguos
            log_files = [
                os.path.join(plugin_dir, 'client.log'),
                os.path.join(plugin_dir, 'plugin_debug.log'),
                os.path.join(plugin_dir, '..', 'plugin_debug.log'),
            ]
            
            # También limpiar logs en el directorio del usuario
            user_temp_dir = os.path.join(os.path.expanduser('~'), '.qgis_detector_palmas')
            if os.path.exists(user_temp_dir):
                log_files.append(os.path.join(user_temp_dir, 'client.log'))
            
            for log_file in log_files:
                if os.path.exists(log_file):
                    try:
                        os.remove(log_file)
                        logger.info(f"Log eliminado: {log_file}")
                    except Exception as e:
                        logger.warning(f"No se pudo eliminar log {log_file}: {str(e)}")
            
            # 2. Limpiar configuraciones problemáticas relacionadas con lotes procesados
            # (pero mantener credenciales si el usuario quiere)
            settings_to_remove = [
                'detector_palmas/saved_lotes_path',
                'detector_palmas/last_lot_id',
                'detector_palmas/last_image_path',
                'detector_palmas/last_output_path',
            ]
            
            for key in settings_to_remove:
                settings.remove(key)
            
            # 3. Guardar la nueva versión
            if current_version:
                settings.setValue('detector_palmas/version', current_version)
            
            logger.info("Limpieza completada. El plugin iniciará con configuración limpia.")
        else:
            # Si es la misma versión, solo limpiar logs muy antiguos (>30 días)
            import time
            current_time = time.time()
            max_log_age = 30 * 24 * 60 * 60  # 30 días en segundos
            
            log_files = [
                os.path.join(plugin_dir, 'client.log'),
                os.path.join(plugin_dir, 'plugin_debug.log'),
            ]
            
            for log_file in log_files:
                if os.path.exists(log_file):
                    try:
                        file_age = current_time - os.path.getmtime(log_file)
                        if file_age > max_log_age:
                            os.remove(log_file)
                            logger.info(f"Log antiguo eliminado (>30 días): {log_file}")
                    except Exception as e:
                        logger.warning(f"No se pudo verificar/eliminar log {log_file}: {str(e)}")
    
    except Exception as e:
        logger.error(f"Error en limpieza de cache y logs: {str(e)}")
        # No fallar la inicialización del plugin si hay error en la limpieza


def apply_detection_styling(layer):
    """
    Estilo estándar para las capas de detección de palmas — el mismo, se
    detecte un lote chico, se una un lote grande en sub-lotes, o se
    unifiquen varias capas después: puntos rojos por cada palma, y una
    etiqueta amarilla con borde negro mostrando el nombre del lote sobre el
    punto especial tipo='lote_label' (uno por lote, en su centro) en vez de
    repetir el nombre en cada palma individual.
    """
    from qgis.core import (QgsRuleBasedRenderer, QgsMarkerSymbol, QgsPalLayerSettings,
                            QgsTextFormat, QgsTextBufferSettings, QgsVectorLayerSimpleLabeling,
                            QgsRuleBasedLabeling)
    from qgis.PyQt.QtGui import QColor, QFont

    field_names = [f.name() for f in layer.fields()]
    has_tipo = "tipo" in field_names
    has_lote = "Lote" in field_names

    # --- Puntos: rojos para cada palma; invisibles para el punto-etiqueta del lote ---
    palma_symbol = QgsMarkerSymbol.createSimple({"name": "circle", "color": "255,0,0,255", "size": "3"})
    if has_tipo:
        root_rule = QgsRuleBasedRenderer.Rule(None)

        palma_rule = QgsRuleBasedRenderer.Rule(palma_symbol.clone())
        palma_rule.setFilterExpression("\"tipo\" = 'palma' OR \"tipo\" IS NULL")
        palma_rule.setLabel("Palma")
        root_rule.appendChild(palma_rule)

        invisible_symbol = QgsMarkerSymbol.createSimple({"name": "circle", "size": "0"})
        label_rule = QgsRuleBasedRenderer.Rule(invisible_symbol.clone())
        label_rule.setFilterExpression("\"tipo\" = 'lote_label'")
        label_rule.setLabel("Lote (etiqueta)")
        root_rule.appendChild(label_rule)

        renderer = QgsRuleBasedRenderer(root_rule)
    else:
        renderer = QgsSingleSymbolRenderer(palma_symbol)
    layer.setRenderer(renderer)

    # --- Etiqueta con el nombre del lote: amarilla, borde negro, solo en el punto-etiqueta ---
    if has_lote:
        text_format = QgsTextFormat()
        text_format.setColor(QColor(255, 255, 0))
        font = QFont("Arial", 10)
        font.setBold(True)
        text_format.setFont(font)
        text_format.setSize(10)

        buffer_settings = QgsTextBufferSettings()
        buffer_settings.setEnabled(True)
        buffer_settings.setSize(1.0)
        buffer_settings.setColor(QColor(0, 0, 0))
        text_format.setBuffer(buffer_settings)

        lote_label_settings = QgsPalLayerSettings()
        lote_label_settings.fieldName = "Lote"
        lote_label_settings.setFormat(text_format)
        lote_label_settings.placement = QgsPalLayerSettings.OverPoint

        if has_tipo:
            lote_rule = QgsRuleBasedLabeling.Rule(lote_label_settings)
            lote_rule.setFilterExpression("\"tipo\" = 'lote_label'")
            root_label_rule = QgsRuleBasedLabeling.Rule(QgsPalLayerSettings())
            root_label_rule.appendChild(lote_rule)
            labeling = QgsRuleBasedLabeling(root_label_rule)
        else:
            labeling = QgsVectorLayerSimpleLabeling(lote_label_settings)

        layer.setLabeling(labeling)
        layer.setLabelsEnabled(True)

    layer.triggerRepaint()


_PALABRAS_FALLA_BACKEND = (
    "no se pudo conectar con el servidor",
    "error de conexión",
    "error del servidor",
    "timeout",
    "no se recibió job_id",
    "no se pudo obtener el resultado",
    "no se pudo completar la subida",
)


def _es_falla_de_backend(exception):
    """True si el error viene de que el servidor de detección no respondió (o
    respondió mal), no de un problema con los datos o la configuración local.

    Se revisa primero el tipo (más confiable), y como respaldo el texto del
    mensaje: el código interno envuelve casi todos los fallos de red en
    Exception genéricas con mensajes en español, así que coincidencias de
    esas frases son la única forma de distinguir "el servidor no contestó" de
    un error de otro tipo sin reescribir cada punto donde se lanza."""
    if isinstance(exception, (requests.exceptions.ConnectionError,
                              requests.exceptions.Timeout,
                              requests.exceptions.RequestException)):
        return True
    texto = str(exception).lower()
    return any(frase in texto for frase in _PALABRAS_FALLA_BACKEND)


def estimar_costo_lote(lotes_path, lot_id, image_path=None):
    """
    Estima cuánto cuesta procesar un lote: (área en ha, bloques, créditos).

    1 bloque = MAX_SUBAREA_HA hectáreas = 1 crédito, así que los tres valores van
    de la mano. Se usa para avisarle al usuario antes de arrancar si le alcanza.
    """
    try:
        import math
        capa = QgsVectorLayer(lotes_path, "lotes_estimacion", "ogr")
        if not capa.isValid():
            return None
        objetivo = None
        for f in capa.getFeatures():
            if str(f.id()) == str(lot_id):
                objetivo = f
                break
        if objetivo is None:
            return None
        detector = LocalDetector()
        area_ha = detector.calculate_lot_area_hectares(objetivo.geometry(), capa.crs())
        if not area_ha:
            return None
        bloques = max(1, int(math.ceil(area_ha / LocalDetector.MAX_SUBAREA_HA)))
        return {"area_ha": area_ha, "bloques": bloques, "creditos": bloques}
    except Exception as e:
        logger.warning(f"No se pudo estimar el costo del lote: {e}")
        return None


def run_detection_headless(image_path, lotes_path, lot_id, progress_callback=None,
                            progress_dialog=None, dockwidget=None, lot_name=None, output_folder=None,
                            reanudar=False):
    """
    Ejecuta una detección sin UI: valida rutas, llama a LocalDetector y carga la capa
    resultante en el proyecto de QGIS con su simbología. No abre ProgressDialog ni
    QMessageBox — pensada para invocarse desde el Asistente (chat) o cualquier
    automatización, con o sin un dockwidget/panel abierto.

    Retorna dict: {"layer_name", "total_detections", "output_shapefile"}.
    """
    # Validaciones de archivos
    if not image_path or not os.path.exists(image_path):
        raise Exception("No se encontró la ortoimagen")
    # No se restringe por extensión: cualquier ráster que GDAL/QGIS pueda abrir
    # (.tif, .ecw, .img, .jp2, .sid, ...) sirve, ya que el recorte y procesamiento
    # posteriores usan QgsRasterLayer/gdalwarp, no un lector de TIFF específico.
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

    logger.info(f"Iniciando nueva detección - Lote FID: {lot_id}")
    logger.info(f"Imagen: {image_path}")
    logger.info(f"Shapefile: {lotes_path}")

    detector = LocalDetector(progress_callback=progress_callback, dockwidget=dockwidget)
    if progress_dialog is not None:
        detector.progress_dialog = progress_dialog
    output_shapefile = detector.process_detection_smart(
        image_path, lotes_path, lot_id, lot_name=lot_name, output_folder=output_folder,
        reanudar=reanudar
    )

    if not os.path.exists(output_shapefile):
        raise Exception(f"No se encontró el archivo de resultados: {output_shapefile}")
    if not output_shapefile.endswith('.shp'):
        raise Exception("El archivo de resultados no es un shapefile válido")

    # Nombre de capa único por ejecución (evitar duplicados al detectar el mismo lote varias veces)
    parent_dir = os.path.basename(os.path.dirname(output_shapefile))
    if parent_dir.startswith("detector_palmas_FID") and "_" in parent_dir:
        layer_name = parent_dir
    else:
        base_name = os.path.basename(output_shapefile).replace('.shp', '')
        layer_name = f"palmas_{lot_id}" if lot_id else f"palmas_{base_name}"
    logger.info(f"Cargando capa con nombre: {layer_name}")
    vlayer = QgsVectorLayer(output_shapefile, layer_name, "ogr")

    if not vlayer.isValid():
        error_msg = f"La capa de puntos no es válida. Detalles:\n"
        error_msg += f"- Ruta: {output_shapefile}\n"
        error_msg += f"- Driver: {vlayer.providerType()}\n"
        error_msg += f"- Error: {vlayer.error().message() if vlayer.error() else 'Desconocido'}"
        raise Exception(error_msg)

    if vlayer.featureCount() == 0:
        raise Exception("La capa no contiene ninguna palma detectada")
    if not vlayer.crs().isValid():
        raise Exception("La capa no tiene un sistema de coordenadas válido")

    QgsProject.instance().addMapLayer(vlayer)

    try:
        apply_detection_styling(vlayer)
        logger.info("Simbología aplicada: puntos rojos + etiqueta de lote en amarillo")
    except Exception as e:
        logger.warning(f"No se pudo aplicar la simbología personalizada: {str(e)}")

    total_detections = vlayer.featureCount()
    logger.info(f"Detección completada: {total_detections} palmas detectadas")
    logger.info(f"Shapefile {output_shapefile} guardado y agregado al panel de capas")

    resultado = {
        "layer_name": layer_name,
        "total_detections": total_detections,
        "output_shapefile": output_shapefile,
    }

    # Si el lote se procesó por bloques y alguno quedó pendiente, se informa hasta
    # dónde se llegó (la capa generada cubre solo los bloques completados).
    pendientes = getattr(detector, "bloques_pendientes", 0)

    if pendientes:
        # Deja anotado en el avance cuál capa quedó incompleta, para poder
        # ofrecer quitarla cuando el lote se termine en una corrida posterior.
        try:
            ruta_estado = _ruta_estado_pendiente(lotes_path, lot_id)
            if os.path.exists(ruta_estado):
                with open(ruta_estado, "r", encoding="utf-8") as f:
                    estado = json.load(f)
                estado["capa_parcial"] = layer_name
                with open(ruta_estado, "w", encoding="utf-8") as f:
                    json.dump(estado, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"No se pudo anotar la capa parcial en el avance: {e}")
    elif getattr(detector, "capa_parcial_previa", ""):
        resultado["capa_parcial_previa"] = detector.capa_parcial_previa

    if pendientes:
        resultado["bloques_pendientes"] = pendientes
        resultado["sublotes_completados"] = detector.sublotes_completados
        resultado["se_puede_reanudar"] = True
        ha_pendientes = pendientes * detector.MAX_SUBAREA_HA
        if detector.detencion_por_creditos:
            resultado["aviso"] = (
                f"Se acabaron los créditos. Quedaron {pendientes} bloques sin procesar "
                f"(≈{ha_pendientes:.0f} ha). La capa creada cubre solo el área ya procesada.\n\n"
                "Al comprar créditos y volver a detectar este lote, el plugin te ofrecerá "
                "continuar donde quedó — no se reprocesa lo ya hecho."
            )
        else:
            resultado["aviso"] = (
                f"Quedaron {pendientes} bloques sin procesar (≈{ha_pendientes:.0f} ha) por errores. "
                "La capa cubre el área procesada; puedes volver a detectar este lote para continuar."
            )
    return resultado


def _preguntar_si_reanudar(dockwidget, lotes_path, lot_id):
    """Si el lote quedó a medias antes, ofrece continuar donde quedó."""
    estado = leer_estado_pendiente(lotes_path, lot_id)
    if not estado:
        return False
    pendientes = len(estado.get("pendientes_wkt", []))
    hechos = len(estado.get("resultados", []))
    respuesta = QMessageBox.question(
        dockwidget,
        "Procesamiento incompleto",
        f"Este lote quedó a medias: {hechos} bloques ya procesados y {pendientes} pendientes "
        f"(≈{pendientes * LocalDetector.MAX_SUBAREA_HA:.0f} ha).\n\n"
        "¿Continuar donde quedó? (No se vuelven a procesar ni a cobrar los bloques ya hechos).\n\n"
        "Si eliges 'No', se procesa el lote completo desde cero.",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.Yes,
    )
    if respuesta == QMessageBox.Yes:
        return True
    borrar_estado_pendiente(lotes_path, lot_id)
    return False


def _confirmar_creditos_suficientes(dockwidget, lotes_path, lot_id, reanudar):
    """
    Avisa antes de arrancar si los créditos no alcanzan para el lote completo,
    diciendo cuánta área sí alcanza a procesar. Devuelve False si el usuario cancela.
    """
    try:
        from ..client_identity import get_credit_balance
        from ..config import CREDITS_PURCHASE_URL

        estimacion = estimar_costo_lote(lotes_path, lot_id)
        if not estimacion:
            return True

        necesarios = estimacion["creditos"]
        if reanudar:
            estado = leer_estado_pendiente(lotes_path, lot_id)
            if estado:
                # El cobro es por área, no por cantidad de bloques: se suma el área
                # que falta y se convierte a créditos igual que lo hace el servidor.
                import math
                from qgis.core import QgsGeometry
                ha_pendientes = 0.0
                for wkt in estado.get("pendientes_wkt", []):
                    geometria = QgsGeometry.fromWkt(wkt)
                    if geometria and not geometria.isEmpty():
                        ha_pendientes += geometria.area() / 10000.0
                if ha_pendientes > 0:
                    necesarios = max(1, int(math.ceil(ha_pendientes / LocalDetector.MAX_SUBAREA_HA)))

        saldo_info = get_credit_balance()
        if saldo_info is None:
            return True  # sin conexión para consultar: que lo resuelva el servidor
        saldo = saldo_info.get("creditos_saldo", 0)
        if saldo >= necesarios:
            return True

        ha_alcanza = saldo * LocalDetector.MAX_SUBAREA_HA
        caja = QMessageBox(dockwidget)
        caja.setIcon(QMessageBox.Warning)
        caja.setWindowTitle("Créditos insuficientes para el lote completo")
        caja.setText(
            f"Este lote necesita {necesarios} créditos ({estimacion['area_ha']:.1f} ha) "
            f"y tienes {saldo}.\n\n"
            f"Puedes procesar ahora unas {ha_alcanza:.0f} ha ({saldo} de {necesarios} bloques) "
            "y continuar después donde quede, sin reprocesar lo ya hecho."
        )
        btn_seguir = caja.addButton("Procesar hasta donde alcance", QMessageBox.AcceptRole)
        btn_comprar = caja.addButton("Comprar créditos", QMessageBox.ActionRole)
        caja.addButton("Cancelar", QMessageBox.RejectRole)
        caja.exec_()

        if caja.clickedButton() is btn_seguir:
            return True
        if caja.clickedButton() is btn_comprar:
            from qgis.PyQt.QtGui import QDesktopServices
            from qgis.PyQt.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(CREDITS_PURCHASE_URL))
        return False
    except Exception as e:
        logger.warning(f"No se pudo verificar créditos antes de procesar: {e}")
        return True


def run_detection(dockwidget):
    """
    Orquesta una detección con UI (ProgressDialog, QMessageBox) sobre `dockwidget`,
    delegando el trabajo real a run_detection_headless().

    Se invoca directamente desde DetectorPalmasDockWidget.run_detection() una vez que ya
    validó autenticación, selección de lote y traslape lote/imagen.
    """
    progress_dialog = None
    lot_id = getattr(dockwidget, 'lot_id', None)
    try:
        image_path = dockwidget.lineEdit.text()
        lotes_path = dockwidget.lineEdit_2.text()
        lot_name = dockwidget.lotNameEdit.text().strip() if hasattr(dockwidget, 'lotNameEdit') else None
        output_folder = dockwidget.outputFolderEdit.text().strip() if hasattr(dockwidget, 'outputFolderEdit') else None

        reanudar = _preguntar_si_reanudar(dockwidget, lotes_path, lot_id)
        if not _confirmar_creditos_suficientes(dockwidget, lotes_path, lot_id, reanudar):
            return

        # Crear y mostrar ventana de progreso
        progress_dialog = ProgressDialog(dockwidget)
        progress_dialog.show()
        progress_dialog.raise_()
        progress_dialog.activateWindow()

        # Mostrar mensaje inicial
        progress_dialog.update_progress(0, "Por favor espere...")
        QApplication.processEvents()

        # Función para actualizar el progreso de forma segura (simplificada - sin porcentajes)
        def update_progress(value, message):
            try:
                logger.debug(f"Actualizando mensaje: {message}")
                if progress_dialog:
                    if not progress_dialog.isVisible():
                        progress_dialog.show()
                        progress_dialog.raise_()
                        progress_dialog.activateWindow()
                    progress_dialog.update_progress(0, message)
                    QApplication.processEvents()
            except Exception as e:
                logger.error(f"Error al actualizar progreso: {str(e)}")
                try:
                    if progress_dialog:
                        progress_dialog.message_label.setText(str(message))
                        QApplication.processEvents()
                except Exception as e2:
                    logger.error(f"Error crítico al actualizar progreso: {str(e2)}")

        result = run_detection_headless(image_path, lotes_path, lot_id,
                                         progress_callback=update_progress,
                                         progress_dialog=progress_dialog,
                                         dockwidget=dockwidget,
                                         lot_name=lot_name,
                                         output_folder=output_folder,
                                         reanudar=reanudar)
        layer_name = result["layer_name"]
        total_detections = result["total_detections"]
        output_shapefile = result["output_shapefile"]

        # Lote terminado tras reanudar: la capa nueva ya cubre todo, así que la
        # incompleta de la corrida anterior solo estorba (mismos puntos repetidos).
        capa_vieja = result.get("capa_parcial_previa")
        if capa_vieja:
            encontradas = QgsProject.instance().mapLayersByName(capa_vieja)
            if encontradas:
                respuesta = QMessageBox.question(
                    dockwidget, "Lote completo",
                    f"El lote quedó completo en la capa nueva.\n\n"
                    f"¿Quitar la capa incompleta anterior ('{capa_vieja}')?\n"
                    "Sus puntos ya están incluidos en la capa nueva.",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if respuesta == QMessageBox.Yes:
                    for capa in encontradas:
                        QgsProject.instance().removeMapLayer(capa.id())

        if result.get("aviso"):
            caja = QMessageBox(dockwidget)
            caja.setIcon(QMessageBox.Warning)
            caja.setWindowTitle("Procesamiento parcial")
            caja.setText(result["aviso"])
            if result.get("bloques_pendientes"):
                btn_comprar = caja.addButton("Comprar créditos", QMessageBox.ActionRole)
            else:
                btn_comprar = None
            caja.addButton("Entendido", QMessageBox.AcceptRole)
            caja.exec_()
            if btn_comprar is not None and caja.clickedButton() is btn_comprar:
                from qgis.PyQt.QtGui import QDesktopServices
                from qgis.PyQt.QtCore import QUrl
                from ..config import CREDITS_PURCHASE_URL
                QDesktopServices.openUrl(QUrl(CREDITS_PURCHASE_URL))

        filename = os.path.basename(output_shapefile)
        final_message = (
            "✅ <b>¡DETECCIÓN COMPLETADA!</b><br><br>"
            f"<b>Total de palmas detectadas:</b> <span style='color:green;font-size:18px'>{total_detections}</span><br>"
            f"<b>Capa agregada:</b> <i>{layer_name}</i><br>"
            f"<b>Archivo:</b> {filename}<br>"
        )
        if hasattr(progress_dialog, 'raise_'):
            progress_dialog.raise_()
            progress_dialog.activateWindow()

        for i in range(3):
            update_progress(100, final_message)
            QApplication.processEvents()
            time.sleep(0.1)

        update_progress(0, "Detección completada exitosamente. Puedes cerrar esta ventana.")
        QApplication.processEvents()
        time.sleep(0.5)

        success_message = (
            f"Detección completada exitosamente\n\n"
            f"Total de palmas detectadas: {total_detections}\n\n"
            f"La capa '{layer_name}' fue agregada al proyecto."
        )
        QMessageBox.information(dockwidget, "Detección Completada", success_message)
        dockwidget.detected_palm_layer_name = layer_name
        logger.info(f"Nombre de capa guardado: {layer_name}")
        if hasattr(dockwidget, 'activatePalmNumberingButton'):
            dockwidget.activatePalmNumberingButton.setVisible(True)
            dockwidget.activatePalmNumberingButton.setEnabled(True)
        if hasattr(dockwidget, 'editPalmsButton'):
            dockwidget.editPalmsButton.setVisible(True)
            dockwidget.editPalmsButton.setEnabled(True)
        if hasattr(dockwidget, 'instructionLabel'):
            dockwidget.instructionLabel.setVisible(False)

        # "Unificar Detecciones" recién tiene sentido con 2 o más capas de puntos
        # ya detectadas — con una sola no hay nada que unir.
        if hasattr(dockwidget, '_update_merge_button_visibility'):
            dockwidget._update_merge_button_visibility()
        elif hasattr(dockwidget, 'mergeDetectionsButton') and hasattr(dockwidget, '_get_point_layers_in_project'):
            try:
                point_layers_count = len(dockwidget._get_point_layers_in_project())
                dockwidget.mergeDetectionsButton.setVisible(point_layers_count >= 2)
            except Exception as e:
                logger.warning(f"No se pudo evaluar la cantidad de capas de puntos: {str(e)}")

    except Exception as e:
        if _es_falla_de_backend(e):
            # El modelo de detección corre en un servidor propio, no en la máquina
            # del usuario: si no responde, lo único accionable para quien usa el
            # plugin es esperar. El detalle técnico (timeout, conexión rechazada,
            # error 500...) queda en el log para quien vaya a revisarlo, pero no
            # tiene sentido mostrárselo al usuario final.
            logger.error(f"Falla de backend en lote {lot_id if lot_id else 'desconocido'}: {str(e)}")
            error_message = (
                "El servicio de detección no está disponible en este momento "
                "(el modelo está en mantenimiento).\n\n"
                "Por favor, intente de nuevo más tarde."
            )
            titulo = "Servicio no disponible"
        else:
            error_message = f"Error en lote {lot_id if lot_id else 'desconocido'}: {str(e)}"
            logger.error(error_message)
            titulo = "Error"
        if progress_dialog:
            progress_dialog.update_progress(0, f"❌ {titulo}\n\n{error_message}")
            QApplication.processEvents()
            time.sleep(3)
        QMessageBox.critical(dockwidget, titulo, error_message)
    finally:
        if progress_dialog:
            QApplication.processEvents()
            time.sleep(0.5)
            progress_dialog.close()

