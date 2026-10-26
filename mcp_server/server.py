"""Servidor MCP propio del laboratorio (Streamable HTTP, puerto 8100).

Expone 5 herramientas: buscar_reglamento, consultar_disponibilidad, crear_reserva,
cancelar_reserva y consultar_auditoria.

Seguridad: toda herramienta exige `ctx_token`, un token HMAC emitido por el orquestador
con la identidad, el rol y el estado de autenticación verificados en el backend. El LLM
no lo ve ni puede fabricarlo. El servidor vuelve a aplicar la política de roles y las
reglas de negocio, así que una llamada MCP directa sin contexto válido es rechazada.

Ejecución:  python -m mcp_server.server
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from labbot import audit, db, faults, security, tool_impl  # noqa: E402
from labbot.domain.policy import Actor, check_tool  # noqa: E402
from labbot.errors import DomainError, ServiceUnavailable  # noqa: E402

mcp = FastMCP(
    "labbot-laboratorio",
    instructions="Herramientas del laboratorio de electrónica. Requieren ctx_token emitido por el orquestador.",
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8100")),
    stateless_http=True,
    json_response=True,
)


def _actor(ctx_token: str) -> tuple[Actor, dict]:
    try:
        claims = security.verify_context(ctx_token)
    except security.TokenError as exc:
        raise PermissionError(f"Contexto inválido: {exc}") from exc
    return Actor(claims["sub"], claims["role"], bool(claims["auth"])), claims


def _run(tool: str, ctx_token: str, args: dict, fn) -> dict:
    started = time.perf_counter()
    try:
        actor, claims = _actor(ctx_token)
    except PermissionError as exc:
        audit.log("mcp_tool_rejected", herramienta=tool, motivo=str(exc))
        return {"status": "denegado", "error": {"code": "CONTEXTO_INVALIDO", "message": str(exc)}}
    decision = check_tool(actor, tool, args)
    base = {"request_id": claims.get("rid"), "session_id": claims.get("sid"), "user_id": actor.user_id}
    if not decision.allowed:
        audit.log("mcp_tool_executed", **base, herramienta=tool, resultado="denegado", motivo=decision.reason,
                  duracion_ms=round((time.perf_counter() - started) * 1000, 1))
        status = "requiere_autenticacion" if decision.code == "REQUIERE_AUTENTICACION" else "denegado"
        return {"status": status, "error": {"code": decision.code, "message": decision.reason}}
    try:
        data = fn(actor)
        result = {"status": "ok", "data": data}
    except DomainError as exc:
        result = {"status": "denegado" if exc.category == "no_autorizado" else "error", "error": exc.to_dict()}
    except ServiceUnavailable as exc:
        audit.log("mcp_tool_executed", **base, herramienta=tool, resultado="servicio_no_disponible", detalle=str(exc))
        raise RuntimeError(f"Dependencia no disponible: {exc.service}") from exc
    audit.log("mcp_tool_executed", **base, herramienta=tool, resultado=result["status"],
              codigo=(result.get("error") or {}).get("code"), modo=args.get("modo"),
              duracion_ms=round((time.perf_counter() - started) * 1000, 1))
    if args.get("modo") == "ejecutar" and faults.active("mcp_drop_response", tool):
        # Fallo inyectado: la operación se ejecutó, pero la respuesta "se pierde".
        raise RuntimeError("Respuesta perdida (fallo inyectado después de ejecutar)")
    return result


@mcp.tool()
def buscar_reglamento(ctx_token: str, consulta: str, max_resultados: int = 4) -> dict:
    """Busca en reglamentos, manuales y procedimientos del laboratorio (RAG). Devuelve fragmentos citables."""
    args = {"consulta": consulta, "max_resultados": max_resultados}
    return _run("buscar_reglamento", ctx_token, args, lambda a: tool_impl.buscar_reglamento(args))


@mcp.tool()
def consultar_disponibilidad(ctx_token: str, fecha: str, hora_inicio: str, hora_fin: str,
                             tipo_equipo: Optional[str] = None, equipo_id: Optional[str] = None) -> dict:
    """Consulta qué equipos están libres en una fecha y horario."""
    args = {"fecha": fecha, "hora_inicio": hora_inicio, "hora_fin": hora_fin, "tipo_equipo": tipo_equipo, "equipo_id": equipo_id}
    return _run("consultar_disponibilidad", ctx_token, args, lambda a: tool_impl.consultar_disponibilidad(args))


@mcp.tool()
def crear_reserva(ctx_token: str, equipo_id: str, fecha: str, hora_inicio: str, hora_fin: str,
                  modo: str = "validar", idempotency_key: Optional[str] = None) -> dict:
    """Valida (modo=validar) o crea (modo=ejecutar) una reserva a nombre del usuario autenticado."""
    args = {"equipo_id": equipo_id, "fecha": fecha, "hora_inicio": hora_inicio, "hora_fin": hora_fin, "modo": modo}
    op = {k: v for k, v in args.items() if k != "modo"}
    return _run("crear_reserva", ctx_token, args,
                lambda a: tool_impl.write_operation(a, "crear_reserva", op, modo, idempotency_key))


@mcp.tool()
def cancelar_reserva(ctx_token: str, reserva_id: str, motivo: Optional[str] = None,
                     modo: str = "validar", idempotency_key: Optional[str] = None) -> dict:
    """Valida o cancela una reserva. Solo el titular (o el encargado) puede cancelarla."""
    args = {"reserva_id": reserva_id, "motivo": motivo, "modo": modo}
    op = {"reserva_id": reserva_id, "motivo": motivo}
    return _run("cancelar_reserva", ctx_token, args,
                lambda a: tool_impl.write_operation(a, "cancelar_reserva", op, modo, idempotency_key))


@mcp.tool()
def consultar_auditoria(ctx_token: str, solicitud_id: Optional[str] = None, sesion_id: Optional[str] = None,
                        usuario_id: Optional[str] = None, tipo_evento: Optional[str] = None, limite: int = 20) -> dict:
    """Consulta eventos de auditoría. El auditor ve todo; los demás roles solo sus propios eventos."""
    args = {"solicitud_id": solicitud_id, "sesion_id": sesion_id, "usuario_id": usuario_id,
            "tipo_evento": tipo_evento, "limite": limite}
    return _run("consultar_auditoria", ctx_token, args, lambda a: tool_impl.consultar_auditoria(a, args))


MCP_TOOLS = ["buscar_reglamento", "consultar_disponibilidad", "crear_reserva", "cancelar_reserva", "consultar_auditoria"]


def main() -> None:
    db.init_schema()
    audit.init()
    from labbot import rag
    rag.retrieve("precarga del índice", 1)  # evita que la primera consulta pague la carga de ChromaDB
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
