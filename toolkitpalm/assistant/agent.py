# -*- coding: utf-8 -*-
"""
Ciclo de conversación del Asistente: recibe un mensaje del usuario, se lo pasa
al modelo de IA elegido junto con el catálogo de acciones de QGIS, ejecuta las
acciones que el modelo pida (en el mismo proceso de QGIS, sin sockets ni
servidores externos) y repite hasta obtener una respuesta final en texto.
"""

import logging

from . import llm_client
from .tools_schema import build_tool_catalog
from .qgis_actions import QgisMCPServer

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = (
    "Eres el Asistente de ToolkitPalm, un plugin de QGIS para palma de aceite. "
    "Puedes controlar QGIS directamente a través de las herramientas disponibles: "
    "consultar y modificar capas, features, estilos, geoprocesos, y ejecutar las "
    "3 herramientas propias del plugin (run_detector, run_segmentador, "
    "run_optimizador). Responde siempre en español, de forma breve y clara. "
    "Antes de ejecutar una acción que modifique datos (borrar, editar, escribir), "
    "confirma que entendiste bien lo que pide el usuario. Si una acción falla, "
    "explica el error en términos simples."
)


class Assistant:
    """Mantiene el historial de conversación y el ejecutor de acciones de QGIS."""

    def __init__(self, iface):
        self.iface = iface
        self.history = []  # formato interno, ver llm_client.py
        # Nunca se llama a .start(): no se abre ningún socket. Se usa únicamente
        # execute_command() como ejecutor de acciones en el mismo proceso.
        self._server = QgisMCPServer(iface=iface)
        self._tools = build_tool_catalog(self._server)

    def reset(self):
        self.history = []

    def send_message(self, user_text, provider_config, attachments=None):
        """
        Envía un mensaje del usuario (con imágenes/documentos adjuntos
        opcionales), ejecuta las acciones que pida el modelo, y devuelve el
        texto final de respuesta (string). Actualiza self.history.

        `attachments`: lista de dicts {"kind": "image"|"document",
        "media_type": str, "data_b64": str, "filename": str}.
        """
        self.history.append({"role": "user", "content": user_text, "attachments": attachments or []})

        for _ in range(MAX_TOOL_ITERATIONS):
            response = llm_client.send(
                provider_config["provider"],
                self.history,
                self._tools,
                system=SYSTEM_PROMPT,
                api_key=provider_config.get("api_key"),
                model=provider_config.get("model"),
                base_url=provider_config.get("base_url"),
            )

            assistant_msg = {"role": "assistant", "content": response.get("text")}
            tool_calls = response.get("tool_calls") or []
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.history.append(assistant_msg)

            if not tool_calls:
                return response.get("text") or ""

            for call in tool_calls:
                result = self._execute_tool(call["name"], call.get("input") or {})
                self.history.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": result,
                })

        return (
            "Se alcanzó el límite de pasos para esta solicitud sin llegar a una "
            "respuesta final. Intenta dividir la petición en pasos más pequeños."
        )

    def _execute_tool(self, name, params):
        logger.info(f"Asistente ejecutando acción: {name} {params}")
        try:
            outcome = self._server.execute_command({"type": name, "params": params})
        except Exception as e:
            logger.exception(f"Error ejecutando acción {name}")
            outcome = {"status": "error", "message": str(e)}
        return outcome
