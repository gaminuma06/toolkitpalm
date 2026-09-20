# -*- coding: utf-8 -*-
"""
Clientes HTTP para hablar con un modelo de IA con "tool calling" (llamado a
funciones), sin depender de los SDK oficiales (`anthropic`/`openai`) para
evitar instalar paquetes nuevos en el Python de QGIS. Se usa `requests`
(ya usado en el resto del plugin) directo contra las APIs REST.

Formato interno de conversación (independiente del proveedor), una lista de:
  {"role": "user", "content": "texto"}
  {"role": "assistant", "content": "texto o None", "tool_calls": [
      {"id": "...", "name": "get_layers", "input": {...}}
  ]}
  {"role": "tool", "tool_call_id": "...", "name": "get_layers", "content": {...}}

Cada función `send_*` recibe esa lista + el catálogo de tools (ver tools_schema.py)
y devuelve: {"text": str|None, "tool_calls": [{"id","name","input"}]}
"""

import json
import logging

import requests

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"


def list_models(api_key, timeout=15):
    """
    Consulta a la API de Anthropic qué modelos de Claude están disponibles en
    este momento (en vez de tener la lista escrita a mano en el código, para
    que se actualice sola cuando Anthropic saque un modelo nuevo o retire uno).
    Devuelve una lista de tuplas (nombre_visible, id_del_modelo).
    """
    if not api_key:
        raise Exception("Falta la clave de API de Claude para consultar los modelos disponibles.")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    response = requests.get(ANTHROPIC_MODELS_URL, headers=headers, timeout=timeout)
    if response.status_code != 200:
        raise Exception(f"Error al consultar los modelos ({response.status_code}): {response.text}")

    data = response.json()
    models = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if not model_id:
            continue
        label = item.get("display_name") or model_id
        models.append((label, model_id))
    return models


def send_claude(messages, tools, system=None, api_key=None, model=None, timeout=120):
    """Llama a la API de Anthropic (Claude) con tool calling."""
    if not api_key:
        raise Exception("Falta la clave de API de Claude (ANTHROPIC_API_KEY).")

    claude_messages = _to_claude_messages(messages)
    claude_tools = [
        {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("parameters", {"type": "object", "properties": {}})}
        for t in tools
    ]

    body = {
        "model": model or DEFAULT_CLAUDE_MODEL,
        "max_tokens": 4096,
        "messages": claude_messages,
        "tools": claude_tools,
    }
    if system:
        body["system"] = system

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=timeout)
    if response.status_code != 200:
        raise Exception(f"Error de la API de Claude ({response.status_code}): {response.text}")

    data = response.json()
    text_parts = []
    tool_calls = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({"id": block.get("id"), "name": block.get("name"), "input": block.get("input", {})})

    return {"text": "\n".join(text_parts) if text_parts else None, "tool_calls": tool_calls}


def _claude_content_blocks(content_text, attachments):
    """
    Construye el "content" de un turno de usuario con imágenes/documentos
    adjuntos. Las imágenes y documentos van antes que el texto (recomendado
    por Anthropic para mejores resultados).
    """
    blocks = []
    for att in attachments or []:
        if att["kind"] == "image":
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": att["media_type"], "data": att["data_b64"]},
            })
        elif att["kind"] == "document":
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": att["media_type"], "data": att["data_b64"]},
            })
    if content_text:
        blocks.append({"type": "text", "text": content_text})
    return blocks


def _to_claude_messages(messages):
    """Convierte el formato interno a la lista de mensajes que espera Claude."""
    claude_messages = []
    pending_tool_results = []

    def flush_tool_results():
        if pending_tool_results:
            claude_messages.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        role = msg["role"]
        if role == "user":
            flush_tool_results()
            attachments = msg.get("attachments")
            if attachments:
                content = _claude_content_blocks(msg["content"], attachments)
            else:
                content = msg["content"]
            claude_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            flush_tool_results()
            content = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls", []):
                content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            claude_messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": json.dumps(msg["content"], ensure_ascii=False, default=str),
            })
    flush_tool_results()
    return claude_messages


def send_openai_compatible(messages, tools, system=None, base_url=None, api_key=None, model=None, timeout=120):
    """
    Llama a cualquier servidor compatible con el formato de chat de OpenAI
    (modelos propios auto-alojados, o proveedores baratos que expongan ese
    mismo formato: Together.ai, Groq, DeepInfra, Ollama, vLLM, etc.).
    """
    if not base_url:
        raise Exception("Falta la URL del servidor del modelo (base_url).")
    if not model:
        raise Exception("Falta el nombre del modelo.")

    oa_messages = []
    if system:
        oa_messages.append({"role": "system", "content": system})
    oa_messages.extend(_to_openai_messages(messages))

    oa_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]

    body = {"model": model, "messages": oa_messages, "tools": oa_tools}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    url = base_url.rstrip("/") + "/chat/completions"
    response = requests.post(url, headers=headers, json=body, timeout=timeout)
    if response.status_code != 200:
        raise Exception(f"Error del modelo ({response.status_code}): {response.text}")

    data = response.json()
    choice = data["choices"][0]["message"]
    text = choice.get("content")
    tool_calls = []
    for tc in choice.get("tool_calls") or []:
        try:
            arguments = json.loads(tc["function"]["arguments"]) if tc["function"].get("arguments") else {}
        except (ValueError, KeyError):
            arguments = {}
        tool_calls.append({"id": tc.get("id"), "name": tc["function"]["name"], "input": arguments})

    return {"text": text, "tool_calls": tool_calls}


def _openai_content_blocks(content_text, attachments):
    """
    Construye el "content" de un turno de usuario en formato OpenAI (vision).
    Solo imágenes son soportadas por este formato genérico; los documentos
    (ej. PDF) no tienen un formato estándar entre servidores OpenAI-compatible,
    así que se avisa en el propio mensaje que no se enviaron.
    """
    blocks = []
    if content_text:
        blocks.append({"type": "text", "text": content_text})
    skipped = []
    for att in attachments or []:
        if att["kind"] == "image":
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{att['media_type']};base64,{att['data_b64']}"},
            })
        else:
            skipped.append(att.get("filename", "archivo"))
    if skipped:
        blocks.append({
            "type": "text",
            "text": (
                "[Nota: este modelo propio solo admite imágenes, así que no se enviaron estos "
                "documentos: " + ", ".join(skipped) + "]"
            ),
        })
    return blocks


def _to_openai_messages(messages):
    """Convierte el formato interno a la lista de mensajes estilo OpenAI."""
    oa_messages = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            attachments = msg.get("attachments")
            if attachments:
                content = _openai_content_blocks(msg["content"], attachments)
            else:
                content = msg["content"]
            oa_messages.append({"role": "user", "content": content})
        elif role == "assistant":
            entry = {"role": "assistant", "content": msg.get("content")}
            if msg.get("tool_calls"):
                entry["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["input"], ensure_ascii=False, default=str)},
                    }
                    for tc in msg["tool_calls"]
                ]
            oa_messages.append(entry)
        elif role == "tool":
            oa_messages.append({
                "role": "tool",
                "tool_call_id": msg["tool_call_id"],
                "content": json.dumps(msg["content"], ensure_ascii=False, default=str),
            })
    return oa_messages


def send(provider, messages, tools, system=None, api_key=None, model=None, base_url=None, timeout=120):
    """Punto de entrada único: despacha al proveedor elegido ('claude' u 'openai_compatible')."""
    if provider == "claude":
        return send_claude(messages, tools, system=system, api_key=api_key, model=model, timeout=timeout)
    elif provider == "openai_compatible":
        return send_openai_compatible(messages, tools, system=system, base_url=base_url, api_key=api_key, model=model, timeout=timeout)
    raise Exception(f"Proveedor de IA desconocido: {provider}")
