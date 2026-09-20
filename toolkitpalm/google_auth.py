# -*- coding: utf-8 -*-
"""
Inicio de sesión con cuenta de Google para ToolkitPalm.

Usa el flujo OAuth 2.0 para aplicaciones de escritorio con PKCE:

  1. Se abre el navegador del usuario en la pantalla de Google.
  2. Google redirige a http://127.0.0.1:<puerto> — un servidor mínimo que el
     plugin levanta solo durante el login — con un código de un solo uso.
  3. El backend cambia ese código por tokens (id_token, refresh_token) y se los
     devuelve al plugin.

Por qué así y no pidiendo usuario/contraseña: el plugin nunca ve la contraseña
del usuario, y el `id_token` que entrega Google lo puede verificar el backend
por su cuenta (viene firmado por Google), así que no hace falta repartir ninguna
clave secreta compartida dentro del plugin — que es justo lo que no se puede
hacer en algo que se publica.

Dos cosas protegen el flujo:

  - PKCE: el par verificador/desafío se genera nuevo en cada inicio de sesión,
    así que un código de autorización interceptado no le sirve a nadie más.
  - El paso 3 ocurre en el servidor, no acá: el `client_secret` de Google nunca
    viaja dentro del plugin publicado (ver _pedir_al_servidor).
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from qgis.PyQt.QtCore import QSettings

logger = logging.getLogger(__name__)

_AUTORIZACION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_ALCANCES = "openid email profile"

_CLAVE_REFRESH = "ToolkitPalm/google_refresh_token"
_CLAVE_PERFIL = "ToolkitPalm/google_perfil"       # JSON: sub, email, name
_AJUSTE_SESION_ID = "ToolkitPalm/google_id_token"
_CLAVE_EXPIRA = "ToolkitPalm/google_id_token_expira"  # epoch en segundos


def _credenciales():
    """(client_id, client_secret) de la app de Google.

    El client_secret siempre sale vacío: ya no vive en el plugin. Se mantiene la
    forma de dos valores para no romper a quien todavía lo tenga en su
    config_secrets.py local.
    """
    from .config import GOOGLE_CLIENT_ID
    return GOOGLE_CLIENT_ID, ""


def _pedir_al_servidor(accion: str, datos: dict) -> dict:
    """Le pide al backend que complete el intercambio con Google.

    El backend es el único que tiene el client_secret; el plugin solo aporta el
    código de autorización y el verificador PKCE que él mismo generó.
    """
    import requests
    from .config import GOOGLE_OAUTH_TOKEN_ENDPOINT, GOOGLE_OAUTH_REFRESCAR_ENDPOINT

    url = GOOGLE_OAUTH_TOKEN_ENDPOINT if accion == "token" else GOOGLE_OAUTH_REFRESCAR_ENDPOINT
    respuesta = requests.post(url, data=datos, timeout=30)
    if respuesta.status_code != 200:
        detalle = respuesta.text[:200]
        try:
            detalle = respuesta.json().get("detail", detalle)
        except Exception:
            logging.getLogger(__name__).debug(
                "Fallo no crítico; se continúa.", exc_info=True)
        raise Exception(f"No se pudo completar el inicio de sesión: {detalle}")
    return respuesta.json()


def esta_configurado() -> bool:
    """True si hay credenciales de Google puestas (sin esto no se puede iniciar sesión)."""
    client_id, _ = _credenciales()
    return bool(client_id)


class _ManejadorRedireccion(BaseHTTPRequestHandler):
    """Recibe la redirección de Google y guarda el código de autorización."""

    codigo = None
    error = None

    def do_GET(self):  # noqa: N802 (nombre impuesto por http.server)
        consulta = urllib.parse.urlparse(self.path).query
        parametros = urllib.parse.parse_qs(consulta)
        _ManejadorRedireccion.codigo = (parametros.get("code") or [None])[0]
        _ManejadorRedireccion.error = (parametros.get("error") or [None])[0]

        if _ManejadorRedireccion.codigo:
            cuerpo = (
                "<h2>Listo</h2><p>Ya puedes volver a QGIS: la sesión quedó iniciada.</p>"
            )
        else:
            cuerpo = (
                "<h2>No se pudo iniciar sesión</h2>"
                f"<p>{_ManejadorRedireccion.error or 'respuesta inesperada de Google'}</p>"
            )
        pagina = f"<html><meta charset='utf-8'><body style='font-family:sans-serif'>{cuerpo}</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(pagina.encode("utf-8"))

    def log_message(self, *args):
        pass  # sin ruido en la consola de QGIS


def _generar_pkce():
    verificador = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    resumen = hashlib.sha256(verificador.encode()).digest()
    desafio = base64.urlsafe_b64encode(resumen).decode().rstrip("=")
    return verificador, desafio


def _leer_payload_id_token(id_token: str) -> dict:
    """Lee los datos del id_token sin verificar la firma.

    La verificación de verdad la hace el backend (que sí puede confiar en ella);
    acá solo se usa para mostrar el nombre y el correo en la interfaz.
    """
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:
        logger.warning(f"No se pudo leer el contenido del id_token: {e}")
        return {}


def _guardar_sesion(respuesta: dict):
    ajustes = QSettings()
    id_token = respuesta.get("id_token", "")
    if id_token:
        ajustes.setValue(_AJUSTE_SESION_ID, id_token)
        ajustes.setValue(_CLAVE_EXPIRA, time.time() + int(respuesta.get("expires_in", 3600)) - 60)
        datos = _leer_payload_id_token(id_token)
        ajustes.setValue(_CLAVE_PERFIL, json.dumps({
            "sub": datos.get("sub", ""),
            "email": datos.get("email", ""),
            "name": datos.get("name", "") or datos.get("given_name", ""),
        }))
    if respuesta.get("refresh_token"):
        ajustes.setValue(_CLAVE_REFRESH, respuesta["refresh_token"])


def iniciar_sesion(timeout_segundos: int = 180, al_esperar=None):
    """
    Abre el navegador para que el usuario inicie sesión con Google.

    `al_esperar` se llama repetidamente mientras se espera la respuesta (sirve
    para refrescar la interfaz y que no se vea congelada).

    Devuelve el perfil {"sub", "email", "name"} o None si no se completó.
    """
    import requests
    from qgis.PyQt.QtGui import QDesktopServices
    from qgis.PyQt.QtCore import QUrl

    client_id, client_secret = _credenciales()
    if not client_id:
        raise Exception(
            "Falta configurar las credenciales de Google del plugin "
            "(GOOGLE_CLIENT_ID en config.py o config_secrets.py)."
        )

    verificador, desafio = _generar_pkce()
    estado = secrets.token_urlsafe(16)

    _ManejadorRedireccion.codigo = None
    _ManejadorRedireccion.error = None
    servidor = HTTPServer(("127.0.0.1", 0), _ManejadorRedireccion)
    puerto = servidor.server_address[1]
    hilo = threading.Thread(target=servidor.handle_request, daemon=True)
    hilo.start()

    redireccion = f"http://127.0.0.1:{puerto}"
    parametros = {
        "client_id": client_id,
        "redirect_uri": redireccion,
        "response_type": "code",
        "scope": _ALCANCES,
        "code_challenge": desafio,
        "code_challenge_method": "S256",
        "state": estado,
        "access_type": "offline",     # para obtener refresh_token
        "prompt": "consent",
    }
    QDesktopServices.openUrl(QUrl(f"{_AUTORIZACION_URL}?{urllib.parse.urlencode(parametros)}"))

    limite = time.time() + timeout_segundos
    while time.time() < limite and _ManejadorRedireccion.codigo is None and _ManejadorRedireccion.error is None:
        if al_esperar:
            al_esperar()
        time.sleep(0.2)

    try:
        servidor.server_close()
    except Exception:
        logging.getLogger(__name__).debug(
            "Fallo no crítico; se continúa.", exc_info=True)

    if _ManejadorRedireccion.error:
        raise Exception(f"Google rechazó el inicio de sesión: {_ManejadorRedireccion.error}")
    if not _ManejadorRedireccion.codigo:
        raise Exception("Se agotó el tiempo de espera del inicio de sesión.")

    # El canje del código lo hace el servidor, no el plugin: ahí vive el
    # client_secret de Google, y así no viaja dentro de algo publicado.
    _guardar_sesion(_pedir_al_servidor("token", {
        "code": _ManejadorRedireccion.codigo,
        "code_verifier": verificador,
        "redirect_uri": redireccion,
    }))
    return perfil()


def perfil():
    """Datos de la cuenta con sesión iniciada, o None."""
    crudo = QSettings().value(_CLAVE_PERFIL, "", type=str)
    if not crudo:
        return None
    try:
        return json.loads(crudo)
    except Exception:
        return None


def sesion_activa() -> bool:
    return perfil() is not None and bool(QSettings().value(_CLAVE_REFRESH, "", type=str))


def cerrar_sesion():
    ajustes = QSettings()
    for clave in (_CLAVE_REFRESH, _CLAVE_PERFIL, _AJUSTE_SESION_ID, _CLAVE_EXPIRA):
        ajustes.remove(clave)


def obtener_id_token():
    """
    Token firmado por Google que identifica al usuario ante el backend.

    Se renueva solo cuando está por vencer (duran ~1 hora). Devuelve None si no
    hay sesión o si no se pudo renovar.
    """
    ajustes = QSettings()
    token = ajustes.value(_AJUSTE_SESION_ID, "", type=str)
    expira = ajustes.value(_CLAVE_EXPIRA, 0.0, type=float)
    if token and time.time() < expira:
        return token

    refresh = ajustes.value(_CLAVE_REFRESH, "", type=str)
    if not refresh:
        return None

    try:
        _guardar_sesion(_pedir_al_servidor("refrescar", {"refresh_token": refresh}))
        return ajustes.value(_AJUSTE_SESION_ID, "", type=str) or None
    except Exception as e:
        logger.warning(f"Error renovando la sesión de Google: {e}")
        return None
