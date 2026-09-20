# -*- coding: utf-8 -*-
"""
Ilustraciones del instructivo, descargadas bajo demanda.

Los GIFs del instructivo pesan unos 56 MB entre todos. El repositorio oficial de
complementos de QGIS acepta 20 MB por paquete, así que no pueden viajar dentro
del plugin. Tampoco corresponde meterlos en el contenedor del servidor: engordan
la imagen y se vuelven a subir en cada despliegue, cuando son archivos estáticos
que no cambian.

Se sirven entonces desde almacenamiento estático y se descargan la primera vez
que el usuario abre el instructivo, quedando guardados en
~/.toolkitpalm/instructivo/ para las veces siguientes.

Si no hay internet no pasa nada grave: el instructivo muestra el texto de cada
paso igual, solo sin la ilustración.
"""
import logging
import os

from qgis.PyQt.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# Base pública donde viven las ilustraciones. Se puede sobreescribir con una
# variable de entorno para probar contra otra ubicación sin tocar el código.
BASE_URL = os.environ.get(
    "TOOLKITPALM_RECURSOS_URL",
    "https://storage.googleapis.com/toolkitpalm-recursos/instructivo",
)

CARPETA_CACHE = os.path.join(os.path.expanduser("~"), ".toolkitpalm", "instructivo")

# Tamaño máximo que se acepta por archivo. Es una guarda simple para no quedarse
# descargando indefinidamente si la URL devolviera algo que no corresponde.
_MAXIMO_BYTES = 30 * 1024 * 1024


def ruta_en_cache(nombre_archivo: str):
    """Ruta local del recurso si ya está descargado, o None."""
    ruta = os.path.join(CARPETA_CACHE, nombre_archivo)
    return ruta if os.path.exists(ruta) and os.path.getsize(ruta) > 0 else None


class DescargaRecurso(QThread):
    """Baja una ilustración en segundo plano, sin congelar la interfaz."""

    listo = pyqtSignal(str)   # ruta local, o "" si no se pudo

    def __init__(self, nombre_archivo, parent=None):
        super().__init__(parent)
        self._nombre = nombre_archivo

    def run(self):
        try:
            import requests

            os.makedirs(CARPETA_CACHE, exist_ok=True)
            destino = os.path.join(CARPETA_CACHE, self._nombre)
            # Se escribe primero a un archivo temporal: si la descarga se corta
            # a la mitad, no queda un GIF truncado en la caché que después se
            # daría por bueno y se mostraría roto.
            parcial = destino + ".parcial"

            respuesta = requests.get(f"{BASE_URL}/{self._nombre}", timeout=60, stream=True)
            if respuesta.status_code != 200:
                logger.warning(f"No se pudo descargar {self._nombre}: HTTP {respuesta.status_code}")
                self.listo.emit("")
                return

            escrito = 0
            with open(parcial, "wb") as f:
                for trozo in respuesta.iter_content(chunk_size=64 * 1024):
                    if not trozo:
                        continue
                    escrito += len(trozo)
                    if escrito > _MAXIMO_BYTES:
                        raise Exception("el archivo supera el tamaño esperado")
                    f.write(trozo)

            os.replace(parcial, destino)
            self.listo.emit(destino)
        except Exception as e:
            logger.warning(f"Falló la descarga de {self._nombre}: {e}")
            try:
                os.remove(parcial)
            except Exception:
                logging.getLogger(__name__).debug(
                    "Fallo no crítico; se continúa.", exc_info=True)
            self.listo.emit("")
