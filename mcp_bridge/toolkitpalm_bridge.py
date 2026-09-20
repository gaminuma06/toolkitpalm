# -*- coding: utf-8 -*-
"""
Puente MCP de ToolkitPalm — Ingeniero Adán Arias.

Este script NO es parte del plugin de QGIS (no se instala dentro de QGIS ni
usa su Python). Es un programa aparte que Claude Desktop o Claude Code lanzan
por su cuenta cuando quieres hablarle a QGIS desde fuera, usando el protocolo
estándar de Anthropic "Model Context Protocol" (MCP) — así puedes usar tu
suscripción de Claude en vez de pagar por la API.

Cómo funciona:
    Claude Desktop/Code  <-- MCP (stdio) -->  este script  <-- socket TCP -->  QGIS
                                                                (ToolkitPalm,
                                                                pestaña Asistente,
                                                                "Conexión externa")

El script solo traduce: recibe una llamada a una herramienta desde Claude,
la reenvía por un socket normal al servidor que ya vive dentro de QGIS
(el mismo motor que usa el chat integrado del plugin), y devuelve la
respuesta. No tiene lógica de QGIS propia — todo el trabajo real lo hace
QGIS del otro lado del socket.

Instalación (una sola vez, en un Python normal — NO en el de QGIS):
    pip install -r requirements.txt

Uso: ver las instrucciones que genera el botón "Copiar instrucciones de
conexión externa" en la pestaña Asistente del plugin — ahí se arma el
comando/JSON exacto con las rutas de tu equipo.
"""

import asyncio
import json
import logging
import os
import socket
import struct
import sys

try:
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server import Server
except ImportError:
    sys.stderr.write(
        "Falta el paquete 'mcp'. Instálalo con:\n"
        "    pip install -r requirements.txt\n"
        "(en el mismo Python que se indicó al configurar Claude Desktop/Code, "
        "NO en el Python de QGIS)\n"
    )
    raise

_HEADER_STRUCT = struct.Struct(">I")
# IMPORTANTE: "127.0.0.1" explícito, NO "localhost". En Windows, resolver
# "localhost" suele probar primero la dirección IPv6 (::1) — donde QGIS no
# escucha, porque su servidor solo abre un socket IPv4 — y recién después de
# esperar ~2 segundos cae a la dirección IPv4 correcta. Ese retraso hacía que
# Claude Desktop diera por perdida la respuesta antes de que llegara.
_HOST = "127.0.0.1"
_CONNECT_TIMEOUT = 5.0
_CALL_TIMEOUT = 120.0

# Registro propio en un archivo, para poder ver exactamente qué pasó sin
# depender de los registros (más limitados) que muestra Claude Desktop.
_LOG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "toolkitpalm_bridge.log")
)
logging.basicConfig(
    filename=_LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("toolkitpalm_bridge")


def _resolve_port():
    """Puerto configurado con --port, o TOOLKITPALM_MCP_PORT, o 9876 por defecto.

    Debe coincidir con el puerto activado en la pestaña Asistente de QGIS.
    """
    import os

    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            return int(args[i + 1])
    return int(os.environ.get("TOOLKITPALM_MCP_PORT", "9876"))


_PORT = _resolve_port()


def _send_to_qgis_sync(command, timeout=_CALL_TIMEOUT):
    """Abre una conexión nueva, envía un comando y devuelve la respuesta de QGIS.

    Se abre una conexión por llamada (en vez de mantener una sola abierta)
    para que este puente no se caiga si QGIS se reinicia, o si el usuario
    apaga/prende la conexión externa entre una llamada y otra.
    """
    try:
        sock = socket.create_connection((_HOST, _PORT), timeout=_CONNECT_TIMEOUT)
    except OSError as e:
        logger.warning("No se pudo conectar a %s:%s -> %r", _HOST, _PORT, e)
        raise ConnectionError(
            f"No se pudo conectar con QGIS en {_HOST}:{_PORT} ({e}). Verifica que QGIS "
            "esté abierto y que la conexión externa esté activada en la pestaña Asistente."
        ) from e
    logger.info("Conectado a QGIS en %s:%s para el comando %r", _HOST, _PORT, command.get("type"))

    try:
        sock.settimeout(timeout)
        # La clave de acceso va en cada orden: QGIS rechaza las que no la traen.
        # Se lee del entorno para no dejarla escrita en ningún archivo.
        token = os.environ.get("QGIS_MCP_TOKEN", "").strip()
        if token:
            command = dict(command, token=token)
        body = json.dumps(command, ensure_ascii=False).encode("utf-8")
        sock.sendall(_HEADER_STRUCT.pack(len(body)) + body)

        buf = b""
        while len(buf) < 4:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("QGIS cerró la conexión sin responder.")
            buf += chunk
        msg_len = _HEADER_STRUCT.unpack(buf[:4])[0]
        buf = buf[4:]
        while len(buf) < msg_len:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("La conexión con QGIS se interrumpió a mitad de la respuesta.")
            buf += chunk
        return json.loads(buf[:msg_len].decode("utf-8"))
    finally:
        sock.close()


async def _send_to_qgis(command, timeout=_CALL_TIMEOUT):
    # El socket es bloqueante; lo corremos en un hilo aparte para no congelar
    # el bucle de eventos de asyncio mientras se espera la respuesta de QGIS.
    return await asyncio.to_thread(_send_to_qgis_sync, command, timeout)


async def handle_list_tools(ctx, params):
    """Pide a QGIS su catálogo de herramientas y lo traduce al formato MCP."""
    logger.info("Claude pidió la lista de herramientas (tools/list)")
    try:
        catalog = await _send_to_qgis({"type": "list_tools", "params": {}})
    except Exception as e:
        logger.warning("list_tools: sin conexión con QGIS (%r); devolviendo herramienta de aviso", e)
        # Se devuelve como si fuera una única "herramienta" para que el mensaje
        # de error sea visible directamente en la conversación de Claude.
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="toolkitpalm_sin_conexion",
                    description=f"No se pudo conectar con QGIS: {e}",
                    input_schema={"type": "object", "properties": {}},
                )
            ]
        )

    # QGIS envuelve la respuesta de cada comando en {"status": "success",
    # "result": <lo que devolvió el handler>} — nuestro list_tools() del lado
    # de QGIS devuelve {"tools": [...]}, así que hay que sacarlo de ["result"].
    payload = catalog.get("result") or {}

    tools = []
    for tool in payload.get("tools", []):
        tools.append(
            types.Tool(
                name=tool["name"],
                description=tool.get("description") or tool["name"],
                input_schema=tool.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    if tools:
        logger.info("list_tools: devolviendo %d herramientas reales de QGIS", len(tools))
    else:
        # Si viene en 0, casi siempre es porque QGIS respondió con un error
        # (comando desconocido, excepción, etc.) en vez de con el catálogo —
        # se deja la respuesta cruda de QGIS en el registro para verla directo.
        logger.warning("list_tools: QGIS no devolvió ninguna herramienta. Respuesta cruda: %r", catalog)
    return types.ListToolsResult(tools=tools)


async def handle_call_tool(ctx, params):
    """Reenvía una llamada de herramienta a QGIS y devuelve el resultado."""
    logger.info("Claude quiere ejecutar la herramienta %r con %r", params.name, params.arguments)
    try:
        response = await _send_to_qgis({"type": params.name, "params": params.arguments or {}})
    except Exception as e:
        logger.warning("call_tool %r: error de conexión -> %r", params.name, e)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Error de conexión con QGIS: {e}")],
            is_error=True,
        )

    is_error = isinstance(response, dict) and response.get("status") == "error"
    logger.info("call_tool %r: respuesta de QGIS recibida (is_error=%s)", params.name, is_error)
    # QGIS envuelve cada respuesta en {"status": "success", "result": ...} o
    # {"status": "error", "message": ...} — se le pasa a Claude solo la parte
    # útil (el resultado, o el mensaje de error), sin el sobre.
    if isinstance(response, dict):
        payload = response.get("message", response) if is_error else response.get("result", response)
    else:
        payload = response
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


server = Server(
    "toolkitpalm",
    version="1.0.0",
    description="Puente hacia QGIS (plugin ToolkitPalm) para Claude Desktop/Code.",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


async def _run():
    logger.info("Puente ToolkitPalm iniciando. Conectará a QGIS en %s:%s", _HOST, _PORT)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_run())
