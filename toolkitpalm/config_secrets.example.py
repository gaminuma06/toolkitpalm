# -*- coding: utf-8 -*-
"""
Credenciales locales (NO COMITEAR).

Copiar este archivo como `config_secrets.py` (mismo directorio) y completar valores.
Este archivo de ejemplo sí se puede commitear.
"""

# API key para hablar con el backend propio (header X-API-Key).
# Debe coincidir con la que configure el backend (proyecto_replicacion/backend).
API_KEY = ""

# API key para los endpoints de autenticación de usuarios. Si se deja vacío,
# config.py usa el mismo valor que API_KEY.
AUTH_API_KEY = ""

# Clave de API de Anthropic (Claude), para el Asistente con chat integrado.
# Se puede configurar también desde la propia pestaña "Asistente" en QGIS.
ANTHROPIC_API_KEY = ""

# Clave de API para un modelo propio/económico (servidor con API compatible con
# el formato de chat de OpenAI). Puede quedar vacío si el servidor no exige clave.
ASSISTANT_CUSTOM_API_KEY = ""
