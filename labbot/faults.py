"""Inyección de fallos para demostrar el escenario D (fallo parcial).

El estado vive en var/faults.json para que lo compartan el backend, el servidor MCP
y el servicio facial, que corren en procesos distintos. Fallos soportados:

- db_down:            la base de datos relacional deja de responder.
- mcp_down:           el servidor MCP no responde (también se puede detener el proceso).
- mcp_drop_response:  lista de herramientas MCP que ejecutan la operación pero pierden
                      la respuesta (el cliente ve un error aunque el cambio se haya hecho).
- face_down:          el servicio de autenticación facial no responde.
- llm_down:           el LLM no responde.
"""
from __future__ import annotations

import json

from . import config

KNOWN = {"db_down", "mcp_down", "mcp_drop_response", "face_down", "llm_down"}


def load() -> dict:
    path = config.faults_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def active(name: str, tool: str | None = None) -> bool:
    value = load().get(name)
    if isinstance(value, list):
        return tool in value
    return bool(value)


def set_fault(name: str, value) -> dict:
    if name not in KNOWN:
        raise ValueError(f"Fallo desconocido: {name}")
    data = load()
    if value in (None, False, []):
        data.pop(name, None)
    else:
        data[name] = value
    config.faults_path().write_text(json.dumps(data), encoding="utf-8")
    return data


def clear() -> None:
    config.faults_path().write_text("{}", encoding="utf-8")
