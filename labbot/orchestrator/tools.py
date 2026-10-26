"""Catálogo de herramientas y ejecutor con controles.

Cada llamada pasa por la misma tubería, sin importar si la pidió el router o el agente:

  1. ¿Existe la herramienta?                         -> si no, error
  2. Validación de la entrada con Pydantic             -> entrada_invalida
  3. Política de roles + autenticación vigente         -> denegado / requiere_autenticacion
  4. Escritura: validación previa + operación pendiente -> requiere_confirmacion (no se ejecuta)
  5. Lectura: ejecución local o MCP con reintentos     -> ok / error / servicio_no_disponible
  6. Validación de la salida (ToolResult)
  7. Evento de auditoría tool_call con todo lo anterior y la duración
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ValidationError

from .. import audit, auth, config, mcp_client, pending, security, sessions, tool_impl
from ..domain import policy
from ..errors import DomainError, ServiceUnavailable
from ..schemas import (BuscarReglamentoIn, CancelarReservaIn, ConsultarAuditoriaIn, ConsultarDisponibilidadIn,
                       ConsultarMisReservasIn, CrearReservaIn, RegistrarDevolucionIn, RegistrarPrestamoIn,
                       SinParametrosIn, ToolResult)
from .context import RequestContext

READ_RETRIES = 3
BACKOFF = (0.2, 0.5, 1.0)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    transport: str  # "mcp" | "local"
    mutating: bool = False


CATALOG: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("buscar_reglamento", "Busca en reglamentos, manuales y procedimientos del laboratorio. Úsela para requisitos, "
             "restricciones, horarios, sanciones o uso de equipos. Devuelve fragmentos con identificador para citar.",
             BuscarReglamentoIn, "mcp"),
    ToolSpec("consultar_disponibilidad", "Consulta qué equipos están libres en una fecha y horario (por tipo o código).",
             ConsultarDisponibilidadIn, "mcp"),
    ToolSpec("consultar_mis_reservas_prestamos", "Lista las reservas y préstamos del usuario autenticado (o de otro usuario "
             "si es encargado o auditor).", ConsultarMisReservasIn, "local"),
    ToolSpec("crear_reserva", "Prepara una reserva de un equipo a nombre del usuario autenticado. El sistema pedirá "
             "confirmación al usuario antes de registrarla.", CrearReservaIn, "mcp", mutating=True),
    ToolSpec("cancelar_reserva", "Prepara la cancelación de una reserva por su código. El sistema pedirá confirmación.",
             CancelarReservaIn, "mcp", mutating=True),
    ToolSpec("registrar_prestamo", "(Encargado) Prepara el registro del préstamo de un equipo a un estudiante. "
             "Requiere confirmación.", RegistrarPrestamoIn, "local", mutating=True),
    ToolSpec("registrar_devolucion", "(Encargado) Prepara el registro de la devolución de un préstamo con el estado del "
             "equipo. Requiere confirmación.", RegistrarDevolucionIn, "local", mutating=True),
    ToolSpec("iniciar_autenticacion_facial", "Inicia la verificación facial 1:1 del usuario declarado. Abre la cámara en "
             "la interfaz; el resultado lo verifica el sistema, no el asistente.", SinParametrosIn, "local"),
    ToolSpec("verificar_autenticacion_facial", "Consulta si la verificación facial en curso fue aprobada y la vincula a "
             "la sesión.", SinParametrosIn, "local"),
    ToolSpec("iniciar_autenticacion_alternativa", "Muestra el formulario seguro del código temporal (cuando la "
             "verificación facial falla o el usuario no está enrolado).", SinParametrosIn, "local"),
    ToolSpec("consultar_auditoria", "Consulta el historial de auditoría. El auditor ve todo; otros roles solo lo propio.",
             ConsultarAuditoriaIn, "mcp"),
    ToolSpec("verificar_integridad_auditoria", "(Auditor) Verifica la cadena de hashes y los anclajes del registro de "
             "auditoría.", SinParametrosIn, "local"),
]}


def llm_tools() -> list[dict]:
    """Definiciones en formato de function calling (OpenAI/Ollama)."""
    tools = []
    for spec in CATALOG.values():
        schema = spec.input_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        tools.append({"type": "function", "function": {"name": spec.name, "description": spec.description,
                                                        "parameters": schema}})
    return tools


def _summary(tool: str, p: dict) -> str:
    if tool == "crear_reserva":
        return (f"Reservar {p['equipo_id']} ({p['equipo']}) a nombre de {p['usuario']} ({p['usuario_id']}) "
                f"el {p['inicio'][:10]} de {p['inicio'][11:]} a {p['fin'][11:]}.")
    if tool == "cancelar_reserva":
        return f"Cancelar la reserva {p['reserva_id']} de {p['equipo_id']} ({p['inicio'][:10]} {p['inicio'][11:]}-{p['fin'][11:]})."
    if tool == "registrar_prestamo":
        extra = f" con la reserva {p['reserva_id']}" if p.get("reserva_id") else " (préstamo inmediato)"
        return (f"Registrar préstamo de {p['equipo_id']} ({p['equipo']}) a {p['usuario']} ({p['usuario_id']}){extra}; "
                f"vence {p['vence'].replace('T', ' ')}.")
    if tool == "registrar_devolucion":
        late = " Devolución tardía: se aplicará sanción (Art. 8)." if p.get("devolucion_tardia") else ""
        return f"Registrar devolución de {p['prestamo_id']} ({p['equipo_id']}) en estado '{p['estado_equipo']}'.{late}"
    return f"{tool}: {json.dumps(p, ensure_ascii=False)}"


class ToolExecutor:
    def __init__(self, ctx: RequestContext):
        self.ctx = ctx

    # --- transporte ---------------------------------------------------------
    def _ctx_token(self) -> str:
        s = self.ctx.session
        return security.sign_context(user_id=s.user_id, role=s.role, session_id=s.id,
                                     request_id=self.ctx.request_id, authenticated=s.authenticated)

    def _mcp(self, tool: str, args: dict) -> dict:
        payload = {"ctx_token": self._ctx_token(), **{k: v for k, v in args.items() if v is not None}}
        return mcp_client.call_tool(tool, payload)

    def run_write(self, tool: str, args: dict, modo: str, key: str | None = None) -> dict:
        """Valida o ejecuta una escritura por su transporte. Devuelve {status, data|error}."""
        spec = CATALOG[tool]
        if spec.transport == "mcp":
            extra = {"modo": modo}
            if key:
                extra["idempotency_key"] = key
            return self._mcp(tool, {**args, **extra})
        try:
            return {"status": "ok", "data": tool_impl.write_operation(self.ctx.actor, tool, args, modo, key)}
        except DomainError as exc:
            return {"status": "denegado" if exc.category == "no_autorizado" else "error", "error": exc.to_dict()}

    def _local_read(self, tool: str, args: dict) -> dict:
        actor = self.ctx.actor
        s = self.ctx.session
        if tool == "consultar_mis_reservas_prestamos":
            return tool_impl.consultar_mis_reservas(actor, args)
        if tool == "verificar_integridad_auditoria":
            return tool_impl.verificar_integridad(actor)
        if tool == "iniciar_autenticacion_facial":
            try:
                data = auth.start_face(s, self.ctx.request_id)
            except auth.AuthError as exc:
                self.ctx.ui_actions.append({"tipo": "codigo_alternativo"})
                raise DomainError(exc.code, exc.message + " Se mostró el formulario del código alternativo.") from exc
            self.ctx.ui_actions.append({"tipo": "captura_facial", "verification_id": data["verification_id"],
                                        "desafio": data["desafio"], "face_url": config.face_public_url()})
            self.ctx.auth_required = True
            return {"estado": "captura_iniciada", "pasos": len(data["desafio"]),
                    "mensaje": "La interfaz abrió la cámara. El sistema verificará el rostro; el asistente no puede "
                               "dar por autenticada a la persona."}
        if tool == "verificar_autenticacion_facial":
            try:
                result = auth.complete_face(s, request_id=self.ctx.request_id)
            except auth.AuthError as exc:
                raise DomainError(exc.code, exc.message) from exc
            self._refresh_session()
            return result
        if tool == "iniciar_autenticacion_alternativa":
            self.ctx.ui_actions.append({"tipo": "codigo_alternativo"})
            self.ctx.auth_required = True
            return {"estado": "formulario_mostrado",
                    "mensaje": "El usuario debe escribir su código temporal en el formulario seguro, no en el chat."}
        raise DomainError("HERRAMIENTA_DESCONOCIDA", tool)

    def _refresh_session(self) -> None:
        self.ctx.session = sessions.get_session(self.ctx.session.id)

    def _read(self, spec: ToolSpec, args: dict) -> dict:
        if spec.transport == "mcp":
            return self._mcp(spec.name, args)
        try:
            return {"status": "ok", "data": self._local_read(spec.name, args)}
        except DomainError as exc:
            return {"status": "denegado" if exc.category == "no_autorizado" else "error", "error": exc.to_dict()}

    # --- tubería principal ----------------------------------------------------
    def call(self, name: str, raw_args) -> dict:
        started = time.perf_counter()
        ctx = self.ctx
        record = {"herramienta": name, "transporte": None, "argumentos": None, "autorizacion": None,
                  "intentos": 0, "estado": None}

        def finish(result: dict) -> dict:
            out = ToolResult.model_validate(result).model_dump(exclude_none=True)  # validación de salida
            record["estado"] = out["status"]
            record["codigo"] = (out.get("error") or {}).get("code")
            record["duracion_ms"] = round((time.perf_counter() - started) * 1000, 1)
            ctx.tools.append({"name": name, "status": out["status"], "code": record["codigo"], "args": record["argumentos"]})
            audit.log("tool_call", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id, **record)
            return out

        spec = CATALOG.get(name)
        if spec is None:
            record["autorizacion"] = {"permitido": False, "motivo": "herramienta inexistente"}
            return finish({"status": "error", "error": {"code": "HERRAMIENTA_DESCONOCIDA", "message": f"No existe '{name}'."}})
        record["transporte"] = spec.transport

        # 2. Validación de entrada
        try:
            if isinstance(raw_args, str):
                raw_args = json.loads(raw_args or "{}")
            args = spec.input_model.model_validate(raw_args or {}).model_dump(exclude_none=True)
        except (ValidationError, json.JSONDecodeError) as exc:
            record["argumentos"] = raw_args if isinstance(raw_args, dict) else str(raw_args)[:300]
            record["autorizacion"] = {"permitido": None, "motivo": "no evaluada: entrada inválida"}
            errors = exc.errors(include_url=False) if isinstance(exc, ValidationError) else [str(exc)]
            return finish({"status": "entrada_invalida",
                           "error": {"code": "ENTRADA_INVALIDA", "message": json.dumps(errors, ensure_ascii=False, default=str)[:600]}})
        record["argumentos"] = args

        # 3. Autorización (rol + autenticación vigente). Se refresca la sesión: la verdad está en el backend.
        self._refresh_session()
        decision = policy.check_tool(ctx.actor, name, args)
        record["autorizacion"] = {"permitido": decision.allowed, "motivo": decision.reason, "codigo": decision.code,
                                  "autenticado": ctx.session.authenticated, "metodo_autenticacion": ctx.session.auth_method,
                                  "rol": ctx.session.role}
        if not decision.allowed:
            if decision.code == "REQUIERE_AUTENTICACION":
                ctx.auth_required = True
                return finish({"status": "requiere_autenticacion", "error": {"code": decision.code, "message": decision.reason},
                               "mensaje": "Llame a iniciar_autenticacion_facial; el sistema verificará la identidad."})
            ctx.denials.append({"tool": name, "code": decision.code, "reason": decision.reason})
            audit.log("authz_denied", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                      herramienta=name, argumentos=args, motivo=decision.reason, codigo=decision.code)
            return finish({"status": "denegado", "error": {"code": decision.code, "message": decision.reason}})

        # 4. Escritura: validar y dejar pendiente de confirmación.
        if spec.mutating:
            if ctx.pending is not None:
                return finish({"status": "error", "error": {"code": "UNA_OPERACION_POR_TURNO",
                               "message": "Ya hay una operación esperando confirmación en este turno."}})
            record["intentos"] = 1
            try:
                preview = self.run_write(name, args, "validar")
            except ServiceUnavailable as exc:
                ctx.service_failures.append({"tool": name, "service": exc.service, "phase": "validar"})
                return finish({"status": "servicio_no_disponible",
                               "error": {"code": "SERVICIO_NO_DISPONIBLE", "message": f"{exc.service} no responde."},
                               "mensaje": "No se realizó ningún cambio."})
            if preview["status"] != "ok":
                self._note_domain_rejection(name, args, preview)
                return finish(preview)
            existing = pending.find_same(ctx.session.id, name, args)
            p = existing or pending.create(ctx.session.id, ctx.request_id, name, args, _summary(name, preview["data"]["resumen"]))
            ctx.pending = p
            if not existing:
                audit.log("confirmation_requested", request_id=ctx.request_id, session_id=ctx.session.id,
                          user_id=ctx.session.user_id, pendiente_id=p["id"], herramienta=name, resumen=p["summary"],
                          clave_idempotencia=p["idempotency_key"])
            record["confirmacion"] = {"requerida": True, "pendiente_id": p["id"]}
            return finish({"status": "requiere_confirmacion",
                           "data": {"pendiente_id": p["id"], "resumen": p["summary"]},
                           "mensaje": "NO se ha ejecutado. El usuario debe confirmar explícitamente; el sistema la "
                                      "ejecutará solo entonces."})

        # 5. Lectura con reintentos ante fallos transitorios.
        last_exc = None
        for attempt in range(1, READ_RETRIES + 1):
            record["intentos"] = attempt
            try:
                result = self._read(spec, args)
                break
            except ServiceUnavailable as exc:
                last_exc = exc
                audit.log("retry", request_id=ctx.request_id, session_id=ctx.session.id, user_id=ctx.session.user_id,
                          herramienta=name, intento=attempt, servicio=exc.service, detalle=exc.detail)
                if attempt < READ_RETRIES:
                    time.sleep(BACKOFF[attempt - 1])
        else:
            ctx.service_failures.append({"tool": name, "service": last_exc.service, "phase": "lectura"})
            return finish({"status": "servicio_no_disponible",
                           "error": {"code": "SERVICIO_NO_DISPONIBLE", "message": f"{last_exc.service} no responde."}})

        if result.get("status") != "ok":
            self._note_domain_rejection(name, args, result)
            return finish(result)
        if name == "buscar_reglamento":
            self._register_evidence(result)
        return finish(result)

    def _note_domain_rejection(self, name: str, args: dict, result: dict) -> None:
        err = result.get("error") or {}
        if result.get("status") == "denegado":
            self.ctx.denials.append({"tool": name, "code": err.get("code"), "reason": err.get("message")})
            audit.log("authz_denied", request_id=self.ctx.request_id, session_id=self.ctx.session.id,
                      user_id=self.ctx.session.user_id, herramienta=name, argumentos=args,
                      motivo=err.get("message"), codigo=err.get("code"), origen="dominio")
        elif result.get("status") == "requiere_autenticacion" or name in policy.AUTH_TOOLS:
            self.ctx.auth_required = True
        else:
            self.ctx.denials.append({"tool": name, "code": err.get("code"), "reason": err.get("message"), "rule": True})

    def _register_evidence(self, result: dict) -> None:
        frags = result["data"]["fragmentos"]
        usable = []
        for f in frags:
            if f["cuarentena"]:
                self.ctx.quarantined.append(f["chunk_id"])
            else:
                self.ctx.evidence[f["chunk_id"]] = f
                usable.append(f["chunk_id"])
        # Los fragmentos en cuarentena no se entregan al LLM.
        result["data"]["fragmentos"] = [f for f in frags if not f["cuarentena"]]
        if any(f["cuarentena"] for f in frags):
            result["data"]["fragmentos_retenidos"] = len(frags) - len(result["data"]["fragmentos"])
        audit.log("rag_evidence", request_id=self.ctx.request_id, session_id=self.ctx.session.id,
                  user_id=self.ctx.session.user_id, fragmentos=[{"id": f["chunk_id"], "score": f["score"],
                                                                 "version": f["version"], "cuarentena": f["cuarentena"],
                                                                 "patrones": f["patrones"]} for f in frags])
