"""Estrategia 1: router con flujos predefinidos.

1. Clasificación: el LLM elige una ruta (salida JSON validada con RouteDecision). Si la
   salida no es válida, se usa el clasificador por palabras clave.
2. Extracción de parámetros: el LLM devuelve JSON (Slots); el analizador determinista
   completa o corrige fechas y horas.
3. Flujo fijo por ruta: la secuencia de herramientas está programada; el LLM solo
   redacta la respuesta documental. Predecible y barato, pero rígido.
"""
from __future__ import annotations

import json

from pydantic import ValidationError

from .. import audit, config, nlp
from ..llm import get_llm
from ..schemas import RouteDecision, Slots
from . import prompts
from .context import RequestContext
from .tools import ToolExecutor


def _llm_json(ctx: RequestContext, system: str, user: str) -> dict | None:
    res = get_llm().chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True,
                         max_tokens=300)
    ctx.add_usage(res.tokens_in, res.tokens_out, res.latency_ms)
    audit.log("llm_call", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
              proposito=system[:28], modelo=res.model, tokens_entrada=res.tokens_in, tokens_salida=res.tokens_out,
              latencia_ms=res.latency_ms)
    try:
        text = (res.text or "").strip()
        text = text[text.find("{"): text.rfind("}") + 1]
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def explain(result: dict) -> str:
    status = result.get("status")
    err = result.get("error") or {}
    # Referencia de la regla aplicada por el backend (no es una cita RAG, por eso va sin corchetes).
    art = f" (regla {err['articulo']})" if err.get("articulo") else ""
    if status == "denegado":
        return f"Operación rechazada: {err.get('message')}{art} El intento quedó registrado en la auditoría."
    if status == "servicio_no_disponible":
        return ("No pude completar la solicitud: " + (err.get("message") or "un servicio no responde") +
                " No se realizó ningún cambio. Intente de nuevo en unos minutos.")
    if status == "entrada_invalida":
        return "Los datos de la solicitud no son válidos. Revise el formato de fechas (AAAA-MM-DD) y horas (HH:MM)."
    if status == "requiere_autenticacion":
        return "Primero necesito verificar su identidad."
    return f"No se pudo completar: {err.get('message')}{art}"


class RouterStrategy:
    name = "router"

    def run(self, ctx: RequestContext, text: str, history: list[dict], ex: ToolExecutor) -> str:
        decision_raw = _llm_json(ctx, prompts.CLASSIFY_SYSTEM, f"Mensaje del usuario: {text}")
        try:
            decision = RouteDecision.model_validate(decision_raw)
            source = "llm"
        except ValidationError:
            decision = RouteDecision(ruta=nlp.classify(text), confianza=0.5)
            source = "palabras_clave"
        ctx.route = decision.ruta
        audit.log("route_selected", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                  estrategia="router", ruta=decision.ruta, confianza=decision.confianza, fuente=source)

        slots = nlp.extract(text, config.now())
        if decision.ruta not in ("consulta_reglamento", "general", "autenticacion"):
            raw = _llm_json(ctx, prompts.extract_system(), f"Mensaje del usuario: {text}") or {}
            try:
                llm_slots = Slots.model_validate({k: v for k, v in raw.items() if k in Slots.model_fields}).model_dump()
            except ValidationError:
                llm_slots = {}
            # El analizador determinista prevalece en fechas/horas; el LLM completa lo que falte.
            for k, v in llm_slots.items():
                if v and not slots.get(k):
                    slots[k] = v
        return getattr(self, "flow_" + decision.ruta)(ctx, text, slots, ex)

    # --- flujos ---------------------------------------------------------------
    def flow_consulta_reglamento(self, ctx, text, s, ex):
        r = ex.call("buscar_reglamento", {"consulta": text})
        if r["status"] != "ok":
            return explain(r)
        frags = r["data"]["fragmentos"]
        if not frags:
            return "No encontré información sobre eso en los documentos oficiales del laboratorio."
        res = get_llm().chat([{"role": "system", "content": prompts.DOC_SYSTEM},
                              {"role": "user", "content": prompts.doc_user(text, frags)}], max_tokens=500)
        ctx.add_usage(res.tokens_in, res.tokens_out, res.latency_ms)
        audit.log("llm_call", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                  proposito="respuesta_documental", modelo=res.model, tokens_entrada=res.tokens_in,
                  tokens_salida=res.tokens_out, latencia_ms=res.latency_ms)
        return res.text or "No pude redactar la respuesta."

    def _slot_args(self, s):
        return {"fecha": s["fecha"], "hora_inicio": s["hora_inicio"], "hora_fin": s["hora_fin"]}

    def flow_consultar_disponibilidad(self, ctx, text, s, ex):
        if not (s["fecha"] and s["hora_inicio"] and s["hora_fin"]):
            return "Indique la fecha y el horario (inicio y fin) que le interesa."
        args = self._slot_args(s) | {k: s[k] for k in ("tipo_equipo", "equipo_id") if s.get(k)}
        r = ex.call("consultar_disponibilidad", args)
        if r["status"] != "ok":
            return explain(r)
        free = [e for e in r["data"]["equipos"] if e["disponible"]]
        if not free:
            return f"No hay equipos disponibles el {s['fecha']} de {s['hora_inicio']} a {s['hora_fin']}."
        return (f"Disponibles el {s['fecha']} de {s['hora_inicio']} a {s['hora_fin']}: " +
                ", ".join(f"{e['equipo_id']} ({e['nombre']})" for e in free) + ".")

    def flow_crear_reserva(self, ctx, text, s, ex):
        if not (s["fecha"] and s["hora_inicio"] and s["hora_fin"]):
            return "Para reservar indique el equipo o tipo de equipo, la fecha y el horario (inicio y fin)."
        equipo = s.get("equipo_id")
        if not equipo:
            if not s.get("tipo_equipo"):
                return "¿Qué tipo de equipo desea reservar (osciloscopio, multímetro, fuente, generador o analizador)?"
            r = ex.call("consultar_disponibilidad", self._slot_args(s) | {"tipo_equipo": s["tipo_equipo"]})
            if r["status"] != "ok":
                return explain(r)
            free = [e for e in r["data"]["equipos"] if e["disponible"]]
            if not free:
                return f"No hay {s['tipo_equipo']}s disponibles el {s['fecha']} de {s['hora_inicio']} a {s['hora_fin']}."
            equipo = free[0]["equipo_id"]
        r = ex.call("crear_reserva", {"equipo_id": equipo, **self._slot_args(s)})
        if r["status"] == "requiere_autenticacion":
            return self._ask_auth(ctx, ex, f"Encontré {equipo} libre. Para reservarlo a su nombre debo verificar su identidad.")
        if r["status"] == "requiere_confirmacion":
            return f"Resumen: {r['data']['resumen']} ¿Confirma la reserva? Responda «confirmo» o use el botón."
        return explain(r)

    def _ask_auth(self, ctx, ex, prefix: str) -> str:
        a = ex.call("iniciar_autenticacion_facial", {})
        if a["status"] == "ok":
            return prefix + " Se abrió la verificación facial: siga las indicaciones de la cámara y luego repita su solicitud."
        return prefix + " " + explain(a) + " Use el código temporal en el formulario seguro."

    def flow_cancelar_reserva(self, ctx, text, s, ex):
        if not s.get("reserva_id"):
            r = ex.call("consultar_mis_reservas_prestamos", {})
            if r["status"] == "requiere_autenticacion":
                return self._ask_auth(ctx, ex, "Para cancelar una reserva debo verificar su identidad.")
            if r["status"] != "ok":
                return explain(r)
            items = ", ".join(f"{x['reserva_id']} ({x['equipo_id']} {x['inicio']})" for x in r["data"]["reservas"])
            return f"Indique el código de la reserva a cancelar. Sus reservas activas: {items or 'ninguna'}."
        r = ex.call("cancelar_reserva", {"reserva_id": s["reserva_id"]})
        if r["status"] == "requiere_autenticacion":
            return self._ask_auth(ctx, ex, "Para cancelar una reserva debo verificar su identidad.")
        if r["status"] == "requiere_confirmacion":
            return f"Resumen: {r['data']['resumen']} ¿Confirma la cancelación?"
        return explain(r)

    def flow_mis_reservas(self, ctx, text, s, ex):
        args = {"usuario_id": s["usuario_id"]} if s.get("usuario_id") else {}
        r = ex.call("consultar_mis_reservas_prestamos", args)
        if r["status"] == "requiere_autenticacion":
            return self._ask_auth(ctx, ex, "Para ver sus reservas debo verificar su identidad.")
        if r["status"] != "ok":
            return explain(r)
        d = r["data"]
        res = "; ".join(f"{x['reserva_id']}: {x['equipo_id']} {x['inicio'].replace('T', ' ')}-{x['fin'][11:]}" for x in d["reservas"])
        loans = "; ".join(f"{x['prestamo_id']}: {x['equipo_id']} (vence {x['vence'].replace('T', ' ')})" for x in d["prestamos"])
        return f"{d['nombre']} — reservas activas: {res or 'ninguna'}. Préstamos activos: {loans or 'ninguno'}."

    def flow_registrar_prestamo(self, ctx, text, s, ex):
        if not (s.get("usuario_id") and s.get("equipo_id")):
            return "Indique el código del estudiante (p. ej. est-001) y del equipo (p. ej. OSC-01)."
        args = {"usuario_id": s["usuario_id"], "equipo_id": s["equipo_id"]}
        if s.get("reserva_id"):
            args["reserva_id"] = s["reserva_id"]
        r = ex.call("registrar_prestamo", args)
        if r["status"] == "requiere_autenticacion":
            return self._ask_auth(ctx, ex, "Para registrar préstamos debo verificar su identidad.")
        if r["status"] == "requiere_confirmacion":
            return f"Resumen: {r['data']['resumen']} ¿Confirma el préstamo?"
        return explain(r)

    def flow_registrar_devolucion(self, ctx, text, s, ex):
        if not s.get("prestamo_id"):
            return "Indique el código del préstamo (p. ej. PRE-0001) y el estado del equipo."
        r = ex.call("registrar_devolucion", {"prestamo_id": s["prestamo_id"], "estado": s.get("estado") or "bueno"})
        if r["status"] == "requiere_autenticacion":
            return self._ask_auth(ctx, ex, "Para registrar devoluciones debo verificar su identidad.")
        if r["status"] == "requiere_confirmacion":
            return f"Resumen: {r['data']['resumen']} ¿Confirma la devolución?"
        return explain(r)

    def flow_auditoria(self, ctx, text, s, ex):
        if s.get("integridad"):
            r = ex.call("verificar_integridad_auditoria", {})
            if r["status"] == "requiere_autenticacion":
                return self._ask_auth(ctx, ex, "Para consultar la auditoría debo verificar su identidad.")
            if r["status"] != "ok":
                return explain(r)
            d = r["data"]
            estado = "ÍNTEGRO" if d["integro"] else f"ALTERADO ({len(d['problemas'])} problemas)"
            return (f"Registro de auditoría {estado}: {d['eventos']} eventos, {d['anclajes_verificados']}/"
                    f"{d['anclajes_total']} anclajes verificados, {d['eventos_sin_anclar']} eventos aún sin anclar.")
        args = {k: s[k] for k in ("solicitud_id", "usuario_id") if s.get(k)}
        r = ex.call("consultar_auditoria", args)
        if r["status"] == "requiere_autenticacion":
            return self._ask_auth(ctx, ex, "Para consultar la auditoría debo verificar su identidad.")
        if r["status"] != "ok":
            return explain(r)
        ev = r["data"]["eventos"]
        lines = [f"#{e['seq']} {e['ts'][11:19]} {e['event_type']} {e['request_id'] or ''}" for e in ev[-10:]]
        return f"Auditoría ({r['data']['alcance']}), últimos {len(lines)} de {len(ev)} eventos:\n" + "\n".join(lines)

    def flow_autenticacion(self, ctx, text, s, ex):
        if s.get("alternativa"):
            ex.call("iniciar_autenticacion_alternativa", {})
            return "Escriba su código temporal de 6 dígitos en el formulario seguro (no en el chat)."
        if ctx.session.face_verification_id:
            r = ex.call("verificar_autenticacion_facial", {})
            if r["status"] == "ok" and r["data"].get("autenticado"):
                return "Su identidad fue verificada por el sistema."
        return self._ask_auth(ctx, ex, "De acuerdo.")

    def flow_general(self, ctx, text, s, ex):
        return ("Puedo ayudarle a consultar reglamentos, ver la disponibilidad de equipos, crear o cancelar reservas, "
                "registrar préstamos y devoluciones (encargado) y consultar la auditoría (auditor).")
