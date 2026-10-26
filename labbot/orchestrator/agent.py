"""Estrategia 2: agente con selección dinámica de herramientas.

El LLM recibe el catálogo completo de herramientas y decide en cada paso cuál llamar
(bucle ReAct sin exponer razonamientos). Más flexible para solicitudes de varios pasos,
pero más costoso y menos predecible. Toda llamada pasa por el mismo ejecutor con
controles que usa el router, así que las reglas de acceso son idénticas.
"""
from __future__ import annotations

import json

from .. import audit
from ..llm import get_llm
from . import prompts
from .context import RequestContext
from .tools import ToolExecutor, llm_tools

MAX_STEPS = 6
MAX_TOOL_RESULT_CHARS = 6000


def _compact(result: dict) -> str:
    """Serializa el resultado para el LLM sin superar el límite y sin romper el JSON."""
    content = json.dumps(result, ensure_ascii=False)
    if len(content) <= MAX_TOOL_RESULT_CHARS:
        return content
    data = dict(result.get("data") or {})
    for key in ("eventos", "fragmentos", "equipos", "reservas", "prestamos", "problemas"):
        items = data.get(key)
        while isinstance(items, list) and len(items) > 1 and len(json.dumps({**result, "data": data}, ensure_ascii=False)) > MAX_TOOL_RESULT_CHARS:
            items = items[-(len(items) // 2):] if key == "eventos" else items[: len(items) // 2]
            data[key] = items
            data["recortado"] = True
    content = json.dumps({**result, "data": data}, ensure_ascii=False)
    if len(content) > MAX_TOOL_RESULT_CHARS:
        content = json.dumps({"status": result.get("status"), "recortado": True,
                              "resumen": content[: MAX_TOOL_RESULT_CHARS - 200]}, ensure_ascii=False)
    return content


class AgentStrategy:
    name = "agente"

    def run(self, ctx: RequestContext, text: str, history: list[dict], ex: ToolExecutor) -> str:
        ctx.route = "agente_dinamico"
        audit.log("route_selected", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                  estrategia="agente", ruta="agente_dinamico", fuente="llm_con_herramientas")
        messages = [{"role": "system", "content": prompts.agent_system(ctx.session)}, *history,
                    {"role": "user", "content": text}]
        tools = llm_tools()
        llm = get_llm()
        for step in range(1, MAX_STEPS + 1):
            res = llm.chat(messages, tools=tools)
            ctx.add_usage(res.tokens_in, res.tokens_out, res.latency_ms)
            audit.log("llm_call", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                      proposito="agente_paso", paso=step, modelo=res.model, tokens_entrada=res.tokens_in,
                      tokens_salida=res.tokens_out, latencia_ms=res.latency_ms,
                      herramientas_solicitadas=[t.name for t in res.tool_calls])
            if not res.tool_calls:
                return res.text or "No tengo una respuesta."
            messages.append(res.assistant_message())
            for call in res.tool_calls:
                result = ex.call(call.name, call.arguments)
                content = _compact(result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        audit.log("agent_step_limit", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                  pasos=MAX_STEPS)
        return "No pude completar la solicitud en el número máximo de pasos. No se realizó ningún cambio sin su confirmación."
