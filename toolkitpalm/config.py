# -*- coding: utf-8 -*-
"""
Configuración única de ToolkitPalm (detector + segmentador + optimizador).
Apunta al backend propio (proyecto_replicacion/backend), desarrollado de forma independiente.
"""

import os

# URL base del backend propio. Por defecto apunta al backend alojado en Google
# Cloud Run (mientras se define el VPS definitivo) — así el plugin funciona sin
# que el usuario tenga que correr nada localmente. Para desarrollo local, se
# puede seguir corriendo con:
#   uvicorn app.main:app --reload
# desde proyecto_replicacion/backend/, y sobreescribir con la variable de
# entorno TOOLKITPALM_API_BASE_URL=http://127.0.0.1:8000
API_BASE_URL = os.environ.get(
    "TOOLKITPALM_API_BASE_URL",
    "https://toolkitpalm-backend-294851649660.us-central1.run.app"
)

# API Key para llamar a los servicios (header X-API-Key). Override en
# config_secrets.py (no se commitea, ver config_secrets.example.py) o variable de entorno.
API_KEY = os.environ.get("TOOLKITPALM_API_KEY", "cambia-esta-clave-en-config-secrets")

# Autenticación de usuarios: un solo flujo para cualquier usuario registrado,
# sin distinguir roles. Misma API key que el resto de servicios por ahora;
# el backend real (Fase 4) puede separarla si hace falta.
AUTH_API_KEY = os.environ.get("TOOLKITPALM_AUTH_API_KEY", API_KEY)

# Override opcional desde config_secrets.py (archivo local, gitignored)
try:
    from .config_secrets import (  # type: ignore
        API_KEY as _API_KEY,
        AUTH_API_KEY as _AUTH_KEY,
    )
    if _API_KEY:
        API_KEY = _API_KEY
    if _AUTH_KEY:
        AUTH_API_KEY = _AUTH_KEY
except Exception:
    pass

# --- Endpoints de autenticación (backend: app/auth/router.py). Un solo flujo, sin
# distinguir tipos de usuario: cualquiera que esté registrado puede usar la herramienta. ---
AUTH_BASE_URL = f"{API_BASE_URL}/datos_palmicultor"
AUTH_ENDPOINT = f"{AUTH_BASE_URL}/autenticar"
AUTH_TIMEOUT = 30  # segundos
AUTH_RETRY_ATTEMPTS = 2
AUTH_ENABLED = True

# Bypass temporal del login mientras se prueba el resto de la funcionalidad
# (autenticación real queda para la Fase 4). Poner en False para reactivar el login.
SKIP_AUTH_FOR_NOW = True

# --- Asistente (chat integrado con IA en la pestaña "Asistente") ---
# Proveedor por defecto: "claude" (API de Anthropic) u "openai_compatible" (un
# modelo propio/económico alojado en cualquier servidor que hable ese formato).
ASSISTANT_DEFAULT_PROVIDER = os.environ.get("TOOLKITPALM_ASSISTANT_PROVIDER", "claude")

ASSISTANT_CLAUDE_MODEL = os.environ.get("TOOLKITPALM_CLAUDE_MODEL", "claude-sonnet-5")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Modelo propio: cualquier servidor con API compatible con el formato de chat de
# OpenAI (auto-alojado, o un proveedor barato). ASSISTANT_CUSTOM_API_KEY puede
# quedar vacío si el servidor no exige autenticación.
ASSISTANT_CUSTOM_MODEL = os.environ.get("TOOLKITPALM_CUSTOM_MODEL", "")
ASSISTANT_CUSTOM_BASE_URL = os.environ.get("TOOLKITPALM_CUSTOM_BASE_URL", "")
ASSISTANT_CUSTOM_API_KEY = os.environ.get("TOOLKITPALM_CUSTOM_API_KEY", "")

# Override opcional desde config_secrets.py (archivo local, gitignored)
try:
    from .config_secrets import ANTHROPIC_API_KEY as _ANTHROPIC_KEY  # type: ignore
    if _ANTHROPIC_KEY:
        ANTHROPIC_API_KEY = _ANTHROPIC_KEY
except Exception:
    pass

try:
    from .config_secrets import ASSISTANT_CUSTOM_API_KEY as _CUSTOM_KEY  # type: ignore
    if _CUSTOM_KEY:
        ASSISTANT_CUSTOM_API_KEY = _CUSTOM_KEY
except Exception:
    pass

# --- Detector de Palmas (backend: app/detector/router.py, prefijo /apis/detector_palmas) ---
DETECTOR_BASE_URL = f"{API_BASE_URL}/apis/detector_palmas"
DETECT_PALMS_ENDPOINT = f"{DETECTOR_BASE_URL}/detect_palms/"
DETECT_PALMS_ASYNC_ENDPOINT = f"{DETECTOR_BASE_URL}/detect_palms_async/"
DETECTOR_STATUS_ENDPOINT = f"{DETECTOR_BASE_URL}/status"
DETECTOR_RESULT_ENDPOINT = f"{DETECTOR_BASE_URL}/result"
DETECTOR_QUEUE_STATUS_ENDPOINT = f"{DETECTOR_BASE_URL}/queue/status"
DETECTOR_PROGRESS_ENDPOINT = f"{DETECTOR_BASE_URL}/progress"
DETECTOR_QUEUE_POLLING_INTERVAL = 15  # segundos
DETECTOR_QUEUE_TIMEOUT = 1800  # segundos (30 min; backend en Cloud Run, solo CPU)

# --- Segmentador de Palmas (backend: app/segmentador/router.py, prefijo /apis/segmentador_palmas) ---
SEGMENTADOR_BASE_URL = f"{API_BASE_URL}/apis/segmentador_palmas"
SEGMENT_PALMS_ENDPOINT = f"{SEGMENTADOR_BASE_URL}/segment_palms/"
SEGMENT_PALMS_ASYNC_ENDPOINT = f"{SEGMENTADOR_BASE_URL}/segment_palms_async/"
SEGMENTADOR_STATUS_ENDPOINT = f"{SEGMENTADOR_BASE_URL}/status"
SEGMENTADOR_RESULT_ENDPOINT = f"{SEGMENTADOR_BASE_URL}/result"
SEGMENTADOR_QUEUE_STATUS_ENDPOINT = f"{SEGMENTADOR_BASE_URL}/queue/status"
SEGMENTADOR_PROGRESS_ENDPOINT = f"{SEGMENTADOR_BASE_URL}/progress"
SEGMENTADOR_QUEUE_POLLING_INTERVAL = 3  # segundos
SEGMENTADOR_QUEUE_TIMEOUT = 3600  # segundos

# Parámetros por defecto de segmentación (slicing + índices espectrales)
DEFAULT_SLICE_HEIGHT = 1024
DEFAULT_SLICE_WIDTH = 1024
DEFAULT_OVERLAP_RATIO = 0.2
DEFAULT_CONFIDENCE_THRESHOLD = 0.1
DEFAULT_STANDARD_RESOLUTION = 0.03528
DEFAULT_RES_THRESHOLD = 0.05
RESAMPLE_CLIP_TARGET_M_PX = 0.10  # remuestreo local antes de subir; None = no remuestrear

# --- Optimizador de Acopios (backend: app/optimizador/router.py, prefijo /optimizador_acopios) ---
OPTIMIZADOR_BASE_URL = f"{API_BASE_URL}/optimizador_acopios"
OPTIMIZE_ENDPOINT = f"{OPTIMIZADOR_BASE_URL}/optimize"
OPTIMIZADOR_STATUS_ENDPOINT = f"{OPTIMIZADOR_BASE_URL}/status"
OPTIMIZADOR_RESULT_ENDPOINT = f"{OPTIMIZADOR_BASE_URL}/result"

DEFAULT_P_ACOPIOS = 7
DEFAULT_ROAD_INTERVAL_M = 50.0
DEFAULT_TARGET_CRS = "EPSG:3116"
DEFAULT_TIME_LIMIT_S = 600

# --- Inicio de sesión con Google (ver google_auth.py) ---
# El client_id es público por diseño: aparece en la URL que se abre en el
# navegador, así que no tiene sentido esconderlo y viaja dentro del plugin.
#
# El client_secret NO está acá y no debe estarlo. Vive solo en el servidor, que
# es quien completa el intercambio con Google (ver app/auth/google_oauth.py en el
# backend). De esa forma el plugin se puede publicar con el código abierto sin
# que contenga un solo secreto, y las credenciales de Google se pueden rotar sin
# obligar a nadie a actualizar el plugin.
GOOGLE_CLIENT_ID = os.environ.get(
    "TOOLKITPALM_GOOGLE_CLIENT_ID",
    "294851649660-d68oslt3c6b68dj777sakf6fkqtmv8a7.apps.googleusercontent.com")

try:
    from .config_secrets import GOOGLE_CLIENT_ID as _G_ID  # type: ignore
    if _G_ID:
        GOOGLE_CLIENT_ID = _G_ID
except Exception:
    pass

# Endpoints del backend que completan el inicio de sesión con Google.
GOOGLE_OAUTH_TOKEN_ENDPOINT = f"{API_BASE_URL}/apis/auth/google/token"
GOOGLE_OAUTH_REFRESCAR_ENDPOINT = f"{API_BASE_URL}/apis/auth/google/refrescar"

# --- Créditos de procesamiento (backend: app/credits/router.py, prefijo /apis/creditos) ---
CREDITS_BASE_URL = f"{API_BASE_URL}/apis/creditos"
CREDITS_SALDO_ENDPOINT = f"{CREDITS_BASE_URL}/saldo"
CREDITS_REGALAR_ENDPOINT = f"{CREDITS_BASE_URL}/regalar"
CREDITS_CODIGO_CREAR_ENDPOINT = f"{CREDITS_BASE_URL}/codigos/crear"
CREDITS_CODIGO_CANJEAR_ENDPOINT = f"{CREDITS_BASE_URL}/codigos/canjear"

# --- Pagos (backend: app/pagos/router.py) ---
PAGOS_CHECKOUT_ENDPOINT = f"{API_BASE_URL}/apis/pagos/checkout"

# Correos que ven la opción de regalar créditos dentro del plugin. Va acá y no
# en el servidor porque solo controla si el botón aparece; el permiso real lo da
# la clave de administración, que solo existe en el config_secrets del dueño.
ADMIN_EMAILS = [c.strip().lower() for c in
                os.environ.get("TOOLKITPALM_ADMIN_EMAILS", "").split(",") if c.strip()]
ADMIN_API_KEY = os.environ.get("TOOLKITPALM_ADMIN_API_KEY", "")

try:
    from .config_secrets import (  # type: ignore
        ADMIN_EMAILS as _ADMIN_EMAILS,
        ADMIN_API_KEY as _ADMIN_KEY,
    )
    if _ADMIN_EMAILS:
        ADMIN_EMAILS = [c.strip().lower() for c in _ADMIN_EMAILS]
    if _ADMIN_KEY:
        ADMIN_API_KEY = _ADMIN_KEY
except Exception:
    pass
# Enlace de compra de paquetes de créditos (25.000 COP = 60 créditos = hasta 300 ha).
# Placeholder hasta que exista la tienda/pasarela de pago conectada al curso.
CREDITS_PURCHASE_URL = os.environ.get(
    "TOOLKITPALM_CREDITS_PURCHASE_URL",
    "https://toolkitpalm.adanarias.com/comprar-creditos"
)

# --- Fotogrametría (Ortofoto/MDE/Curvas) — EN PLANEACIÓN, sin backend todavía ---
# Ver notas completas en fotogrametria/dockwidget.py. Motor propuesto: OpenDroneMap
# (ODM) corriendo en un backend aparte (ej. VM ARM gratis de Oracle Cloud Free Tier),
# nunca dentro del Python de QGIS. Cuando se desarrolle, seguir el mismo patrón
# async submit -> status -> result que Detector/Segmentador:
# FOTOGRAMETRIA_BASE_URL = f"{API_BASE_URL}/apis/fotogrametria"
# FOTOGRAMETRIA_SUBMIT_ENDPOINT = f"{FOTOGRAMETRIA_BASE_URL}/procesar/"
# FOTOGRAMETRIA_STATUS_ENDPOINT = f"{FOTOGRAMETRIA_BASE_URL}/status"
# FOTOGRAMETRIA_RESULT_ENDPOINT = f"{FOTOGRAMETRIA_BASE_URL}/result"

# --- Común a los 3 módulos ---
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

DEFAULT_WINDOW_WIDTH = 400
DEFAULT_WINDOW_HEIGHT = 650

SUPPORTED_IMAGE_FORMATS = ['.tif', '.tiff', '.ecw', '.img', '.jp2', '.sid']
SUPPORTED_SHAPEFILE_FORMATS = ['.shp']
REQUIRED_SHAPEFILE_EXTENSIONS = ['.shp', '.dbf', '.shx', '.prj']
SUPPORTED_VECTOR_FORMATS = ['.gpkg', '.shp', '.geojson']

UPLOAD_METHOD = "streaming"  # "basic", "streaming", "async"
UPLOAD_TIMEOUT = 300  # segundos
UPLOAD_CHUNK_SIZE = 8192  # bytes
UPLOAD_PROGRESS_INTERVAL = 0.2  # segundos

REQUESTS_TIMEOUT = 300  # segundos
REQUESTS_RETRY_ATTEMPTS = 3
REQUESTS_RETRY_DELAY = 5  # segundos
