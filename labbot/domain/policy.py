"""Política de autorización por roles (RBAC + reglas de propiedad).

La autenticación facial solo prueba *quién* es el usuario; aquí se decide *qué* puede
hacer. Esta política se evalúa en el orquestador antes de llamar a una herramienta y se
vuelve a evaluar dentro del dominio y del servidor MCP (defensa en profundidad). Nada de
esto depende del prompt: un LLM manipulado no puede saltarse estas comprobaciones.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import Forbidden

ROLES = ("estudiante", "encargado", "auditor")

AUTH_TOOLS = {
    "iniciar_autenticacion_facial",
    "verificar_autenticacion_facial",
    "iniciar_autenticacion_alternativa",
}

PERMISSIONS: dict[str, set[str]] = {
    "estudiante": {
        "buscar_reglamento", "consultar_disponibilidad", "consultar_mis_reservas_prestamos",
        "crear_reserva", "cancelar_reserva", "consultar_auditoria", *AUTH_TOOLS,
    },
    "encargado": {
        "buscar_reglamento", "consultar_disponibilidad", "consultar_mis_reservas_prestamos",
        "crear_reserva", "cancelar_reserva", "registrar_prestamo", "registrar_devolucion",
        "consultar_auditoria", *AUTH_TOOLS,
    },
    "auditor": {
        "buscar_reglamento", "consultar_disponibilidad", "consultar_mis_reservas_prestamos",
        "consultar_auditoria", "verificar_integridad_auditoria", *AUTH_TOOLS,
    },
}

# Herramientas que exigen una identidad verificada (facial o alternativa) y vigente.
REQUIRES_AUTH = {
    "consultar_mis_reservas_prestamos", "crear_reserva", "cancelar_reserva",
    "registrar_prestamo", "registrar_devolucion", "consultar_auditoria",
    "verificar_integridad_auditoria",
}

MUTATING = {"crear_reserva", "cancelar_reserva", "registrar_prestamo", "registrar_devolucion"}


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    authenticated: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    code: str = "OK"


def check_tool(actor: Actor, tool: str, args: dict | None = None) -> Decision:
    args = args or {}
    if actor.role not in PERMISSIONS:
        return Decision(False, f"Rol desconocido: {actor.role}", "ROL_INVALIDO")
    if tool not in PERMISSIONS[actor.role]:
        return Decision(False, f"El rol '{actor.role}' no puede usar '{tool}'.", "NO_AUTORIZADO")
    if tool in REQUIRES_AUTH and not actor.authenticated:
        return Decision(False, "Se requiere verificar la identidad antes de esta operación.", "REQUIERE_AUTENTICACION")
    target = args.get("usuario_id")
    if tool == "consultar_mis_reservas_prestamos" and target and target != actor.user_id:
        if actor.role not in ("encargado", "auditor"):
            return Decision(False, "Un estudiante solo puede consultar sus propias reservas y préstamos.", "NO_AUTORIZADO")
    if tool == "consultar_auditoria" and actor.role != "auditor":
        if target and target != actor.user_id:
            return Decision(False, "Solo el auditor puede consultar la auditoría de otros usuarios.", "NO_AUTORIZADO")
    return Decision(True, "Permitido por la política de roles.")


def require(actor: Actor, tool: str, args: dict | None = None) -> None:
    decision = check_tool(actor, tool, args)
    if not decision.allowed:
        raise Forbidden(decision.reason, code=decision.code)


def audit_scope_user(actor: Actor) -> str | None:
    """None = puede ver toda la auditoría; de lo contrario, solo la propia."""
    return None if actor.role == "auditor" else actor.user_id
