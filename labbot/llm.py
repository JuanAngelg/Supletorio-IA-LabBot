"""Integración con el modelo de lenguaje mediante API.

- OpenAICompatLLM: API /v1/chat/completions con function calling. Por defecto apunta a
  Ollama local (http://localhost:11434/v1, modelo qwen2.5:7b); sirve igual para OpenAI u
  otro proveedor compatible cambiando LLM_BASE_URL, LLM_MODEL y LLM_API_KEY.
- MockLLM: determinista, SOLO para pruebas automatizadas y para verificar la tubería sin
  modelo. Sus resultados no sirven para comparar estrategias (ver docs/RESULTADOS.md).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache

from . import config, faults, nlp
from .errors import ServiceUnavailable


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMResult:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    model: str = ""

    def assistant_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.text or ""}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": t.id, "type": "function", "function": {"name": t.name, "arguments": t.arguments}}
                                 for t in self.tool_calls]
        return msg


class OpenAICompatLLM:
    def __init__(self):
        from openai import OpenAI
        self.model = config.llm_model()
        self.client = OpenAI(base_url=config.llm_base_url(), api_key=config.llm_api_key(), timeout=config.llm_timeout(),
                             max_retries=1)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
             max_tokens: int = 700) -> LLMResult:
        if faults.active("llm_down"):
            raise ServiceUnavailable("llm", "fallo inyectado")
        kwargs: dict = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            resp = self.client.chat.completions.create(**kwargs)
        except Exception as exc:  # conexión, tiempo agotado, modelo inexistente...
            raise ServiceUnavailable("llm", f"{exc.__class__.__name__}: {str(exc)[:160]}") from exc
        msg = resp.choices[0].message
        calls = [ToolCall(tc.id or f"call_{uuid.uuid4().hex[:8]}", tc.function.name, tc.function.arguments or "{}")
                 for tc in (msg.tool_calls or [])]
        usage = resp.usage
        return LLMResult(msg.content, calls, getattr(usage, "prompt_tokens", 0) or 0,
                         getattr(usage, "completion_tokens", 0) or 0, round((time.perf_counter() - started) * 1000, 1),
                         self.model)


# ---------------------------------------------------------------------------
class MockLLM:
    """Simulador determinista. Reconoce la tarea por marcadores del prompt de sistema."""

    model = "mock-determinista"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
             max_tokens: int = 700) -> LLMResult:
        if faults.active("llm_down"):
            raise ServiceUnavailable("llm", "fallo inyectado")
        started = time.perf_counter()
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if "[TAREA:CLASIFICAR]" in system:
            out = LLMResult(json.dumps({"ruta": nlp.classify(_last_line(user)), "confianza": 0.9}))
        elif "[TAREA:EXTRAER]" in system:
            out = LLMResult(json.dumps(nlp.extract(_last_line(user), config.now())))
        elif "[TAREA:RESPONDER_DOCUMENTAL]" in system:
            out = LLMResult(_doc_answer(user))
        elif tools:
            out = _agent_step(messages)
        else:
            out = LLMResult("¿En qué puedo ayudarle con los equipos del laboratorio?")
        prompt_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) + len(json.dumps(tools or []))
        out.tokens_in = prompt_chars // 4
        out.tokens_out = len((out.text or "") + "".join(t.arguments for t in out.tool_calls)) // 4 + 5
        out.latency_ms = round((time.perf_counter() - started) * 1000, 2)
        out.model = self.model
        return out


def _last_line(text: str) -> str:
    m = re.search(r"Mensaje del usuario:\s*(.*)", text, re.S)
    return m.group(1).strip() if m else text


def _doc_answer(user: str) -> str:
    frags = re.findall(r'<fragmento id="([^"]+)"[^>]*>(.*?)</fragmento>', user, re.S)
    if not frags:
        return "No encontré información sobre eso en los documentos del laboratorio."
    parts = []
    for cid, body in frags[:2]:
        sentences = re.split(r"(?<=[.:;])\s+", body.strip())
        parts.append(" ".join(sentences[:3]).strip() + f" [{cid}]")
    return "Según la documentación del laboratorio: " + " ".join(parts)


def _call(name: str, args: dict) -> LLMResult:
    return LLMResult(None, [ToolCall(f"call_{uuid.uuid4().hex[:8]}", name, json.dumps(args, ensure_ascii=False))])


def _agent_step(messages: list[dict]) -> LLMResult:
    last_user_idx = max(i for i, m in enumerate(messages) if m["role"] == "user")
    text = messages[last_user_idx]["content"]
    names: dict[str, str] = {}
    results: list[tuple[str, dict]] = []
    for m in messages[last_user_idx + 1:]:
        if m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                names[tc["id"]] = tc["function"]["name"]
        elif m["role"] == "tool":
            results.append((names.get(m["tool_call_id"], "?"), json.loads(m["content"])))
    done = [n for n, _ in results]
    route = nlp.classify(text)
    s = nlp.extract(text, config.now())

    if results:
        name, last = results[-1]
        status = last.get("status")
        if status == "requiere_autenticacion" and "iniciar_autenticacion_facial" not in done:
            return _call("iniciar_autenticacion_facial", {})
        if status != "ok" or name.startswith("iniciar_autenticacion") or name == "verificar_autenticacion_facial":
            return LLMResult(_describe(name, last))

    if route == "consulta_reglamento":
        if "buscar_reglamento" not in done:
            return _call("buscar_reglamento", {"consulta": text})
        frags = results[-1][1]["data"]["fragmentos"]
        body = "".join(f'<fragmento id="{f["chunk_id"]}">{f["text"]}</fragmento>' for f in frags)
        return LLMResult(_doc_answer(body))
    if route in ("consultar_disponibilidad", "crear_reserva"):
        if not (s["fecha"] and s["hora_inicio"] and s["hora_fin"]):
            return LLMResult("Indique la fecha y el horario (inicio y fin) que necesita.")
        slot = {"fecha": s["fecha"], "hora_inicio": s["hora_inicio"], "hora_fin": s["hora_fin"]}
        if route == "crear_reserva" and s["equipo_id"]:
            return _call("crear_reserva", {"equipo_id": s["equipo_id"], **slot})
        if "consultar_disponibilidad" not in done:
            args = {**slot, **({"tipo_equipo": s["tipo_equipo"]} if s["tipo_equipo"] else {})}
            return _call("consultar_disponibilidad", args)
        free = [e for e in results[-1][1]["data"]["equipos"] if e["disponible"]]
        if route == "consultar_disponibilidad":
            return LLMResult("Equipos disponibles: " + (", ".join(f"{e['equipo_id']} ({e['nombre']})" for e in free) or "ninguno") + ".")
        if not free:
            return LLMResult("No hay equipos disponibles en ese horario.")
        return _call("crear_reserva", {"equipo_id": free[0]["equipo_id"], **slot})
    if route == "cancelar_reserva":
        if s["reserva_id"]:
            return _call("cancelar_reserva", {"reserva_id": s["reserva_id"]})
        if "consultar_mis_reservas_prestamos" not in done:
            return _call("consultar_mis_reservas_prestamos", {})
        return LLMResult("Indique el código de la reserva que desea cancelar (por ejemplo RES-0001).")
    if route == "mis_reservas":
        if not done:
            return _call("consultar_mis_reservas_prestamos", {"usuario_id": s["usuario_id"]} if s["usuario_id"] else {})
        return LLMResult(_describe(*results[-1]))
    if route == "registrar_prestamo":
        if not (s["usuario_id"] and s["equipo_id"]):
            return LLMResult("Indique el código del estudiante y del equipo.")
        args = {"usuario_id": s["usuario_id"], "equipo_id": s["equipo_id"]}
        if s["reserva_id"]:
            args["reserva_id"] = s["reserva_id"]
        return _call("registrar_prestamo", args)
    if route == "registrar_devolucion":
        if not s["prestamo_id"]:
            return LLMResult("Indique el código del préstamo (por ejemplo PRE-0001).")
        return _call("registrar_devolucion", {"prestamo_id": s["prestamo_id"], "estado": s["estado"] or "bueno"})
    if route == "auditoria":
        if not done:
            if s["integridad"]:
                return _call("verificar_integridad_auditoria", {})
            return _call("consultar_auditoria", {"solicitud_id": s["solicitud_id"]} if s["solicitud_id"] else {})
        return LLMResult(_describe(*results[-1]))
    if route == "autenticacion":
        return _call("iniciar_autenticacion_alternativa" if s["alternativa"] else "iniciar_autenticacion_facial", {})
    return LLMResult("Puedo ayudarle con reglamentos, disponibilidad, reservas, préstamos, devoluciones y auditoría.")


def _describe(name: str, result: dict) -> str:
    status = result.get("status")
    err = result.get("error") or {}
    if status == "requiere_confirmacion":
        return f"Resumen de la operación: {result['data']['resumen']} ¿Confirma que desea realizarla?"
    if status == "denegado":
        return f"No es posible realizar la operación: {err.get('message')}"
    if status == "requiere_autenticacion":
        return "Primero debe verificar su identidad."
    if status == "servicio_no_disponible":
        return f"No pude completar la solicitud porque un servicio no responde ({err.get('message')}). No se realizó ningún cambio."
    if status in ("error", "entrada_invalida"):
        art = f" ({err['articulo']})" if err.get("articulo") else ""
        return f"No se pudo completar: {err.get('message')}{art}"
    data = result.get("data") or {}
    if name == "iniciar_autenticacion_facial":
        return "Abrí la verificación facial. Siga las indicaciones de la cámara."
    if name == "iniciar_autenticacion_alternativa":
        return "Escriba su código temporal en el formulario seguro que apareció en pantalla."
    if name == "consultar_mis_reservas_prestamos":
        res = ", ".join(f"{r['reserva_id']} {r['equipo_id']} {r['inicio']}" for r in data.get("reservas", [])) or "ninguna"
        loans = ", ".join(f"{p['prestamo_id']} {p['equipo_id']}" for p in data.get("prestamos", [])) or "ninguno"
        return f"Reservas activas: {res}. Préstamos activos: {loans}."
    if name == "consultar_auditoria":
        return f"Se encontraron {len(data.get('eventos', []))} eventos de auditoría ({data.get('alcance')})."
    if name == "verificar_integridad_auditoria":
        return f"Integridad del registro: {'íntegro' if data.get('integro') else 'ALTERADO'}; {data.get('eventos')} eventos."
    return "Listo."


@lru_cache(maxsize=1)
def get_llm():
    return MockLLM() if config.llm_provider() == "mock" else OpenAICompatLLM()
