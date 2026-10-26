"""Orquestador: coordina contexto, autenticación, autorización, ejecución y auditoría.

Responsabilidades que NO delega al LLM:
- Asignar el identificador de solicitud y registrar la solicitud y su resultado final.
- Decidir si un mensaje es una confirmación (regla determinista o botón de la interfaz).
- Ejecutar las operaciones confirmadas con clave de idempotencia, reintentos y
  reconciliación cuando un servicio falla a mitad de camino.
- Validar que las citas de la respuesta existan entre los fragmentos recuperados.
- Impedir que la respuesta afirme operaciones o autenticaciones que el sistema no registró.
"""
from __future__ import annotations

import re
import secrets
import time

from .. import audit, config, guard, pending, rag, sessions
from ..domain import policy, service
from ..errors import DomainError, ServiceUnavailable
from .agent import AgentStrategy
from .context import RequestContext
from .router import RouterStrategy
from .tools import CATALOG, ToolExecutor

STRATEGIES = {"router": RouterStrategy(), "agente": AgentStrategy()}

CONFIRM_RE = re.compile(r"^\s*(s[ií]|confirm[oa]?|confirmar|acepto|de acuerdo|adelante|proceda|hágalo|hagalo|ok|vale|dale)\b"
                        r"[\s,.!¡]*(confirm\w*|por favor|adelante)?[\s.!]*$", re.IGNORECASE)
REJECT_RE = re.compile(r"^\s*(no|cancel\w*|mejor no|anul\w*|det[eé]n\w*|rechaz\w*)\b[\s,.!a-záéíóúñ]{0,30}$", re.IGNORECASE)
CLAIM_OP_RE = re.compile(r"\b(reserva|préstamo|prestamo|devolución|devolucion|cancelación|cancelacion)\b[^.\n]{0,80}?"
                         r"\b(ha sido|fue|quedó|queda|está|esta|se ha|se)\s+(cread|confirmad|registrad|realizad|cancelad|completad|efectuad)", re.IGNORECASE)
CLAIM_AUTH_RE = re.compile(r"\b(est[aá]|ha sido|fue|queda|qued[oó])\s+(autenticad|verificad)", re.IGNORECASE)

WRITE_RETRIES = 3


def _new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


def _cost(tokens_in: int, tokens_out: int) -> float:
    pin, pout = config.price_per_mtok()
    return round(tokens_in / 1e6 * pin + tokens_out / 1e6 * pout, 6)


class Orchestrator:
    # --- entrada principal ---------------------------------------------------
    def handle_message(self, session_id: str, text: str, strategy: str = "router") -> dict:
        session = sessions.get_session(session_id)
        request_id = _new_request_id()
        text = (text or "").strip()[:2000]
        audit.log("request_received", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  solicitud=text, estrategia=strategy, rol=session.role, autenticado=session.authenticated,
                  metodo_autenticacion=session.auth_method if session.authenticated else None)
        flags = guard.scan(text)
        if flags:
            audit.log("input_screening", request_id=request_id, session_id=session.id, user_id=session.user_id,
                      patrones=flags, accion="se procesa con controles normales; no otorga privilegios")
        history = sessions.history(session.id)
        sessions.add_message(session.id, "user", text, request_id)

        open_op = pending.latest_open(session.id)
        if open_op and len(text) <= 60 and CONFIRM_RE.match(text):
            return self._decide(session, open_op, "confirmar", request_id, via="mensaje")
        if open_op and len(text) <= 60 and REJECT_RE.match(text):
            return self._decide(session, open_op, "rechazar", request_id, via="mensaje")

        ctx = RequestContext(request_id=request_id, session=session, strategy=strategy, text=text)
        ex = ToolExecutor(ctx)
        try:
            reply = STRATEGIES[strategy].run(ctx, text, history, ex)
        except ServiceUnavailable as exc:
            ctx.service_failures.append({"service": exc.service, "phase": "estrategia"})
            audit.log("error", request_id=request_id, session_id=session.id, user_id=session.user_id,
                      tipo="servicio_no_disponible", servicio=exc.service, detalle=exc.detail)
            reply = (f"No puedo atender la solicitud ahora: el servicio «{exc.service}» no responde. "
                     "No se realizó ningún cambio.")
        except Exception as exc:  # noqa: BLE001 - se registra y se responde con honestidad
            audit.log("error", request_id=request_id, session_id=session.id, user_id=session.user_id,
                      tipo="excepcion", detalle=f"{exc.__class__.__name__}: {str(exc)[:300]}")
            ctx.service_failures.append({"service": "orquestador", "phase": "excepcion"})
            reply = "Ocurrió un error interno al procesar la solicitud. No se realizó ningún cambio."
        if ctx.route is None:
            audit.log("route_selected", request_id=request_id, session_id=session.id, user_id=session.user_id,
                      estrategia=strategy, ruta="no_determinada", fuente="error_previo")
        reply = self._postprocess(ctx, reply)
        return self._finish(ctx, reply)

    # --- confirmaciones ------------------------------------------------------
    def confirm(self, session_id: str, pending_id: str, decision: str) -> dict:
        session = sessions.get_session(session_id)
        request_id = _new_request_id()
        audit.log("request_received", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  solicitud=f"[botón] {decision} {pending_id}", estrategia="confirmacion", rol=session.role,
                  autenticado=session.authenticated)
        op = pending.get(pending_id)
        if not op or op["session_id"] != session.id:
            ctx = RequestContext(request_id=request_id, session=session, strategy="confirmacion")
            audit.log("confirmation_decision", request_id=request_id, session_id=session.id, user_id=session.user_id,
                      pendiente_id=pending_id, decision=decision, resultado="pendiente_inexistente_o_ajena")
            ctx.denials.append({"code": "PENDIENTE_INVALIDA"})
            return self._finish(ctx, "No existe esa operación pendiente en esta sesión.")
        return self._decide(session, op, "confirmar" if decision == "confirmar" else "rechazar", request_id, via="boton")

    def _decide(self, session, op: dict, decision: str, request_id: str, via: str) -> dict:
        ctx = RequestContext(request_id=request_id, session=session, strategy="confirmacion")
        ctx.route = "confirmacion"
        base = {"request_id": request_id, "session_id": session.id, "user_id": session.user_id}
        audit.log("confirmation_decision", **base, pendiente_id=op["id"], herramienta=op["tool"], decision=decision,
                  via=via, resumen=op["summary"], solicitud_origen=op["request_id"])
        if op["status"] != "pendiente":
            ctx.denials.append({"code": "PENDIENTE_CERRADA"})
            return self._finish(ctx, f"Esa operación ya no está pendiente (estado: {op['status']}).")
        if op["expires_at"] < time.time():
            pending.set_status(op["id"], "expirada")
            ctx.denials.append({"code": "PENDIENTE_EXPIRADA"})
            return self._finish(ctx, "La confirmación venció. Repita la solicitud para generar un nuevo resumen.")
        if decision == "rechazar":
            pending.set_status(op["id"], "rechazada")
            return self._finish(ctx, "Entendido: operación cancelada. No se realizó ningún cambio.")

        # Re-verificación al ejecutar: la autenticación pudo vencer y los permisos se vuelven a comprobar.
        actor = policy.Actor(session.user_id, session.role, session.authenticated)
        decision_authz = policy.check_tool(actor, op["tool"], op["args"])
        audit.log("authz_decision", **base, herramienta=op["tool"], permitido=decision_authz.allowed,
                  motivo=decision_authz.reason, codigo=decision_authz.code, autenticado=session.authenticated,
                  metodo_autenticacion=session.auth_method)
        if not decision_authz.allowed:
            if decision_authz.code == "REQUIERE_AUTENTICACION":
                ctx.auth_required = True
                return self._finish(ctx, "Su verificación de identidad venció. Verifíquese de nuevo y vuelva a confirmar.")
            ctx.denials.append({"code": decision_authz.code})
            pending.set_status(op["id"], "rechazada")
            return self._finish(ctx, f"Operación rechazada: {decision_authz.reason}")

        ex = ToolExecutor(ctx)
        started = time.perf_counter()
        result = None
        last_exc = None
        for attempt in range(1, WRITE_RETRIES + 1):
            try:
                result = ex.run_write(op["tool"], op["args"], "ejecutar", op["idempotency_key"])
                break
            except ServiceUnavailable as exc:
                last_exc = exc
                audit.log("retry", **base, herramienta=op["tool"], intento=attempt, servicio=exc.service,
                          detalle=exc.detail, clave_idempotencia=op["idempotency_key"])
                if attempt < WRITE_RETRIES:
                    time.sleep(0.3 * attempt)
        duration = round((time.perf_counter() - started) * 1000, 1)

        if result is None:
            # Reconciliación: ¿llegó a ejecutarse? La clave de idempotencia lo responde.
            try:
                found = service.lookup_operation(op["idempotency_key"])
                reconciled = "ejecutada" if found else "no_ejecutada"
            except ServiceUnavailable:
                found, reconciled = None, "desconocido"
            audit.log("reconciliation", **base, pendiente_id=op["id"], clave_idempotencia=op["idempotency_key"],
                      resultado=reconciled, servicio_fallido=last_exc.service if last_exc else None)
            if found:
                result = {"status": "ok", "data": {"modo": "ejecutar", "resultado": {**found, "repetida": True,
                                                                                     "reconciliada": True}}}
            else:
                ctx.service_failures.append({"tool": op["tool"], "service": last_exc.service, "phase": "ejecutar",
                                             "reconciliacion": reconciled})
                ctx.tools.append({"name": op["tool"], "status": "servicio_no_disponible", "code": "SERVICIO_NO_DISPONIBLE"})
                audit.log("tool_call", **base, herramienta=op["tool"], transporte=CATALOG[op["tool"]].transport,
                          argumentos=op["args"], autorizacion={"permitido": True, "motivo": decision_authz.reason},
                          intentos=WRITE_RETRIES, estado="servicio_no_disponible", duracion_ms=duration,
                          confirmacion={"pendiente_id": op["id"], "confirmada": True})
                if reconciled == "no_ejecutada":
                    msg = (f"No se pudo completar la operación: el servicio «{last_exc.service}» no responde. "
                           "Comprobé que NO se registró ningún cambio. La operación sigue pendiente: puede volver a "
                           "confirmarla cuando el servicio se restablezca (no se duplicará).")
                else:
                    msg = (f"No puedo confirmar la operación: el servicio «{last_exc.service}» no responde y no es "
                           "posible verificar si se registró. NO la dé por realizada. Vuelva a confirmar más tarde: "
                           "el sistema usará la misma clave y nunca la duplicará.")
                return self._finish(ctx, msg)

        status = result.get("status")
        ctx.tools.append({"name": op["tool"], "status": status, "code": (result.get("error") or {}).get("code")})
        audit.log("tool_call", **base, herramienta=op["tool"], transporte=CATALOG[op["tool"]].transport,
                  argumentos=op["args"], autorizacion={"permitido": True, "motivo": decision_authz.reason},
                  intentos=attempt, estado=status, codigo=(result.get("error") or {}).get("code"),
                  duracion_ms=duration, confirmacion={"pendiente_id": op["id"], "confirmada": True},
                  clave_idempotencia=op["idempotency_key"])
        if status != "ok":
            err = result.get("error") or {}
            pending.set_status(op["id"], "fallida", result)
            if status == "denegado":
                ctx.denials.append({"code": err.get("code")})
                audit.log("authz_denied", **base, herramienta=op["tool"], motivo=err.get("message"),
                          codigo=err.get("code"), origen="dominio")
            else:
                ctx.denials.append({"code": err.get("code"), "rule": True})
            art = f" (regla {err['articulo']})" if err.get("articulo") else ""
            return self._finish(ctx, f"La operación no se realizó: {err.get('message')}{art}")

        data = result["data"]["resultado"]
        pending.set_status(op["id"], "ejecutada", data)
        ctx.mutations.append({"tool": op["tool"], "result": data})
        audit.log("db_change", **base, herramienta=op["tool"], cambios=data.get("cambios"), repetida=data.get("repetida"),
                  reconciliada=data.get("reconciliada", False), clave_idempotencia=op["idempotency_key"])
        return self._finish(ctx, self._success_text(op["tool"], data))

    @staticmethod
    def _success_text(tool: str, d: dict) -> str:
        note = " (reintento detectado: se devolvió el registro original, sin duplicar)" if d.get("repetida") else ""
        if tool == "crear_reserva":
            return (f"Reserva registrada: {d['reserva_id']} — {d['equipo_id']} ({d['equipo']}) el {d['inicio'][:10]} "
                    f"de {d['inicio'][11:]} a {d['fin'][11:]} a nombre de {d['usuario']}.{note}")
        if tool == "cancelar_reserva":
            return f"Reserva {d['reserva_id']} cancelada.{note}"
        if tool == "registrar_prestamo":
            return (f"Préstamo registrado: {d['prestamo_id']} — {d['equipo_id']} a {d['usuario']}; "
                    f"vence {d['vence'].replace('T', ' ')}.{note}")
        if tool == "registrar_devolucion":
            extra = " Se aplicó sanción por devolución tardía (Art. 8)." if d.get("sanciones") else ""
            return f"Devolución registrada: {d['prestamo_id']} en estado '{d['estado_equipo']}'.{extra}{note}"
        return "Operación realizada." + note

    # --- post-procesamiento --------------------------------------------------
    def _postprocess(self, ctx: RequestContext, reply: str) -> str:
        # 1. Citas: solo valen las de fragmentos efectivamente recuperados en esta solicitud.
        for cid in rag.extract_citations(reply):
            if cid in ctx.evidence:
                ctx.citations_valid.append(cid)
            else:
                ctx.citations_invalid.append(cid)
                reply = reply.replace(f"[{cid}]", "[cita no verificada]")
        if ctx.citations_invalid:
            audit.log("citation_check", request_id=ctx.request_id, session_id=ctx.session.id,
                      user_id=ctx.session.user_id, invalidas=ctx.citations_invalid, validas=ctx.citations_valid)
        # 2. Veracidad: el texto no puede afirmar lo que el sistema no registró.
        if not ctx.mutations and CLAIM_OP_RE.search(reply):
            audit.log("truth_guard", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                      motivo="la respuesta afirmaba una operación no ejecutada", texto_original=reply[:500])
            if ctx.pending is not None:
                reply = (f"Resumen: {ctx.pending['summary']} La operación aún NO se ha realizado; "
                         "se ejecutará solo si usted la confirma.")
            else:
                reply = "No se ha realizado ninguna operación en esta solicitud. " + self._status_note(ctx)
        refreshed = sessions.get_session(ctx.session.id)
        if not refreshed.authenticated and CLAIM_AUTH_RE.search(reply) and not re.search(r"\bno\s+(est[aá]|ha sido)", reply):
            audit.log("truth_guard", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                      motivo="la respuesta afirmaba una autenticación no verificada", texto_original=reply[:500])
            reply = "Su identidad NO está verificada por el sistema. " + self._status_note(ctx)
        # 3. Nota determinista del estado real de la operación pendiente.
        if ctx.pending is not None and ctx.pending["summary"] not in reply:
            reply += f"\n\nPendiente de su confirmación: {ctx.pending['summary']}"
        return reply

    @staticmethod
    def _status_note(ctx: RequestContext) -> str:
        if ctx.service_failures:
            return "Un servicio no respondió; no hubo cambios."
        if ctx.denials:
            d = ctx.denials[-1]
            return f"La solicitud fue rechazada ({d.get('code')})."
        if ctx.auth_required:
            return "Primero debe verificar su identidad."
        return "¿Desea que lo intente de nuevo?"

    def _final_status(self, ctx: RequestContext) -> str:
        if ctx.mutations:
            return "completada"
        if ctx.service_failures:
            return "fallo_servicio"
        if ctx.pending is not None:
            return "requiere_confirmacion"
        if any(not d.get("rule") for d in ctx.denials):
            return "rechazada"
        if ctx.denials:
            return "rechazada_por_regla"
        if ctx.auth_required:
            return "requiere_autenticacion"
        return "completada"

    def _finish(self, ctx: RequestContext, reply: str) -> dict:
        status = self._final_status(ctx)
        session = sessions.get_session(ctx.session.id)
        latency = ctx.elapsed_ms()
        audit.log("final_result", request_id=ctx.request_id, session_id=session.id, user_id=session.user_id,
                  estado=status, respuesta=reply, ruta=ctx.route, herramientas=[t["name"] for t in ctx.tools],
                  cambios=[m["result"].get("cambios") for m in ctx.mutations], pendiente_id=(ctx.pending or {}).get("id"),
                  evidencias=list(ctx.evidence), citas_validas=ctx.citations_valid, citas_invalidas=ctx.citations_invalid,
                  fragmentos_en_cuarentena=ctx.quarantined, errores=ctx.service_failures, rechazos=ctx.denials,
                  latencia_ms=latency, llm_llamadas=ctx.llm_calls, llm_ms=round(ctx.llm_ms, 1),
                  tokens_entrada=ctx.tokens_in, tokens_salida=ctx.tokens_out,
                  costo_estimado_usd=_cost(ctx.tokens_in, ctx.tokens_out), autenticado=session.authenticated)
        audit.anchor()
        response = {
            "request_id": ctx.request_id, "respuesta": reply, "estado": status, "ruta": ctx.route,
            "herramientas": ctx.tools, "pendiente": ({"id": ctx.pending["id"], "resumen": ctx.pending["summary"]}
                                                     if ctx.pending else None),
            "acciones_ui": ctx.ui_actions, "autenticacion": session.auth_state(),
            "citas": {"validas": ctx.citations_valid, "invalidas": ctx.citations_invalid},
            "metricas": {"latencia_ms": latency, "tokens_entrada": ctx.tokens_in, "tokens_salida": ctx.tokens_out,
                         "llm_llamadas": ctx.llm_calls, "costo_estimado_usd": _cost(ctx.tokens_in, ctx.tokens_out)},
        }
        sessions.add_message(session.id, "assistant", reply, ctx.request_id,
                             {k: response[k] for k in ("estado", "ruta", "pendiente", "acciones_ui")})
        return response


orchestrator = Orchestrator()
