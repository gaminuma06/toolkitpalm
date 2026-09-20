# -*- coding: utf-8 -*-
"""
Genera toolkitpalm.mcpb: el paquete de extensión que Claude Desktop puede
instalar con un clic (Configuración → Extensiones → Avanzado → "Instalar
extensión..."), sin que el usuario tenga que instalar Python ni ejecutar
`pip install` — Claude Desktop se encarga de todo eso solo (usa el runtime
"uv" que trae incluido).

No requiere Node.js/npm ni la herramienta oficial `mcpb`: un archivo .mcpb es,
según su propia especificación (github.com/modelcontextprotocol/mcpb), sencillamente
un .zip con manifest.json en la raíz — así que se arma directo con la librería
estándar de Python (zipfile).

Uso: correr este script cada vez que cambie toolkitpalm_bridge.py o los
archivos de mcp_bridge/mcpb/, para regenerar el .mcpb.
    python build_mcpb.py
"""

import os
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_MCPB_SRC_DIR = os.path.join(_HERE, "mcpb")
_BRIDGE_SCRIPT = os.path.join(_HERE, "toolkitpalm_bridge.py")
_OUTPUT = os.path.join(_HERE, "toolkitpalm.mcpb")


def build():
    with zipfile.ZipFile(_OUTPUT, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(_MCPB_SRC_DIR, "manifest.json"), "manifest.json")
        zf.write(os.path.join(_MCPB_SRC_DIR, "pyproject.toml"), "pyproject.toml")
        zf.write(_BRIDGE_SCRIPT, "src/toolkitpalm_bridge.py")
    print(f"Listo: {_OUTPUT}")


if __name__ == "__main__":
    build()
