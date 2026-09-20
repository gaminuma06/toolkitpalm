# -*- coding: utf-8 -*-
"""
Identificador de cliente para el sistema de créditos de procesamiento.

Mientras no exista un inicio de sesión real (Google u otro), se usa un UUID
generado una sola vez por instalación y guardado en QSettings — es el mismo
valor que el backend usa como clave del documento en Firestore para llevar el
saldo de créditos. El día que se implemente autenticación real (p. ej. cuenta
de Google), este valor se reemplaza por el correo/ID de esa cuenta sin tener
que cambiar nada del backend: solo cambia qué texto se manda como client_id.
"""
import uuid
import logging
from qgis.PyQt.QtCore import QSettings

_SETTINGS_KEY = "ToolkitPalm/client_id"
_NAME_SETTINGS_KEY = "ToolkitPalm/display_name"

logger = logging.getLogger(__name__)


def get_client_id() -> str:
    """Identificador del cliente para el sistema de créditos.

    Si hay sesión de Google iniciada, manda la cuenta de Google: así el saldo
    sigue al usuario aunque cambie de computador o reinstale el plugin. Si no,
    se usa el UUID local de la instalación (modo sin sesión).
    """
    try:
        from . import google_auth
        datos = google_auth.perfil()
        if datos and datos.get("sub"):
            return f"google:{datos['sub']}"
    except Exception as e:
        logger.warning(f"No se pudo leer la sesión de Google: {e}")

    settings = QSettings()
    client_id = settings.value(_SETTINGS_KEY, "", type=str)
    if not client_id:
        client_id = str(uuid.uuid4())
        settings.setValue(_SETTINGS_KEY, client_id)
    return client_id


def get_display_name() -> str:
    """Nombre que el usuario escribió al "iniciar sesión". Vacío si no ha iniciado sesión."""
    return QSettings().value(_NAME_SETTINGS_KEY, "", type=str)


def set_display_name(name: str):
    QSettings().setValue(_NAME_SETTINGS_KEY, (name or "").strip())


def clear_display_name():
    """"Cerrar sesión": solo oculta el nombre en la interfaz. El client_id (y su saldo
    de créditos en el backend) no se pierde — al volver a iniciar sesión con el mismo
    nombre, sigue siendo el mismo cliente."""
    QSettings().remove(_NAME_SETTINGS_KEY)


def is_logged_in() -> bool:
    return bool(get_display_name())


def cabeceras_autenticacion(extra: dict = None) -> dict:
    """Cabeceras para hablar con el backend, iguales para las 3 herramientas.

    Lo que realmente autoriza es el `id_token` de Google: el servidor lo verifica
    contra Google y de ahí saca quién es el usuario. La X-API-Key se sigue
    mandando por compatibilidad, pero ya no es la que da el permiso — no podría
    serlo, porque viaja dentro de un plugin que se publica con el código abierto
    y cualquiera puede leerla.
    """
    from .config import API_KEY

    cabeceras = {"X-API-Key": API_KEY}
    try:
        from . import google_auth
        token = google_auth.obtener_id_token()
        if token:
            cabeceras["Authorization"] = f"Bearer {token}"
    except Exception as e:
        logger.warning(f"No se pudo adjuntar la sesión de Google: {e}")
    if extra:
        cabeceras.update(extra)
    return cabeceras


def get_credit_balance(timeout: int = 30):
    """Consulta el saldo de créditos del cliente actual contra el backend.
    Devuelve un dict {"creditos_saldo": int, "ha_disponibles": float} o None si
    la consulta falla (sin conexión, servidor caído, etc.) — quien llame debe
    manejar el None mostrando algo como "—" en vez de fallar.

    El tiempo de espera es generoso a propósito: si el servidor estaba apagado
    (Cloud Run lo apaga cuando nadie lo usa), la primera consulta despierta la
    instancia y puede tardar bastante en responder."""
    try:
        import requests
        from .config import CREDITS_SALDO_ENDPOINT, API_KEY
    except Exception as e:
        logger.warning(f"No se pudo importar dependencias para consultar créditos: {e}")
        return None
    client_id = get_client_id()

    # El servidor decide de quién es el saldo por la sesión de Google, no por el
    # identificador de la URL: sin este token responde 401. Se manda igual el
    # client_id porque forma parte de la ruta del endpoint.
    cabeceras = {"X-API-Key": API_KEY}
    try:
        from . import google_auth
        token = google_auth.obtener_id_token()
        if token:
            cabeceras["Authorization"] = f"Bearer {token}"
    except Exception as e:
        logger.warning(f"No se pudo adjuntar la sesión de Google al consultar el saldo: {e}")

    try:
        resp = requests.get(
            f"{CREDITS_SALDO_ENDPOINT}/{client_id}",
            headers=cabeceras,
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"Consulta de saldo respondió {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.warning(f"No se pudo consultar el saldo de créditos: {e}")
    return None
