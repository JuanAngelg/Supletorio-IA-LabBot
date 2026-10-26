"""Cliente MCP del orquestador (Streamable HTTP).

Abre una sesión MCP por llamada (el servidor es stateless) con tiempo límite. Cualquier
fallo de red, tiempo agotado o error del servidor se traduce en ServiceUnavailable: el
orquestador no sabe si la operación llegó a ejecutarse y debe reconciliar.
"""
from __future__ import annotations

import asyncio
import json

from . import config, faults
from .errors import ServiceUnavailable


async def _call_http(name: str, args: dict, timeout: float) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(config.mcp_url(), timeout=timeout, sse_read_timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.isError:
        raise ServiceUnavailable("servidor_mcp", text[:200] or "error en la herramienta")
    if result.structuredContent and "status" in result.structuredContent:
        return result.structuredContent
    return json.loads(text)


async def _list_http() -> list[dict]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(config.mcp_url(), timeout=10) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools.tools]


def call_tool(name: str, args: dict, timeout: float = 15.0) -> dict:
    if faults.active("mcp_down"):
        raise ServiceUnavailable("servidor_mcp", "el servidor MCP no responde (fallo inyectado)")
    if config.mcp_transport() == "inprocess":
        from mcp_server import server  # solo pruebas unitarias
        try:
            return getattr(server, name)(**args)
        except RuntimeError as exc:
            raise ServiceUnavailable("servidor_mcp", str(exc)) from exc
    try:
        return asyncio.run(asyncio.wait_for(_call_http(name, args, timeout), timeout + 2))
    except ServiceUnavailable:
        raise
    except BaseException as exc:  # ExceptionGroup, httpx, timeout, etc.
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ServiceUnavailable("servidor_mcp", f"{exc.__class__.__name__}") from exc


def list_tools() -> list[dict]:
    return asyncio.run(_list_http())
