"""Esquemas estructurados (Pydantic) de entrada y salida de las herramientas.

El LLM recibe el JSON Schema derivado de estos modelos; el orquestador valida cada
llamada antes de ejecutarla y valida también la salida antes de devolverla al LLM.
Ningún esquema expuesto al LLM contiene la identidad del actor, el token de contexto ni
la clave de idempotencia: esos campos los añade el orquestador.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Fecha = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="Fecha en formato AAAA-MM-DD")
Hora = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="Hora en formato 24 h HH:MM")
TipoEquipo = Literal["osciloscopio", "generador", "multimetro", "fuente", "analizador"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuscarReglamentoIn(_In):
    consulta: str = Field(min_length=3, max_length=500, description="Pregunta o tema a buscar en reglamentos y manuales")
    max_resultados: int = Field(default=4, ge=1, le=6)


class ConsultarDisponibilidadIn(_In):
    tipo_equipo: Optional[TipoEquipo] = Field(default=None, description="Tipo de equipo")
    equipo_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z]{3}-\d{2}$", description="Código del equipo, p. ej. OSC-01")
    fecha: str = Fecha
    hora_inicio: str = Hora
    hora_fin: str = Hora


class ConsultarMisReservasIn(_In):
    usuario_id: Optional[str] = Field(default=None, description="Solo encargado o auditor: consultar a otro usuario")
    incluir_historial: bool = False


class CrearReservaIn(_In):
    equipo_id: str = Field(pattern=r"^[A-Za-z]{3}-\d{2}$", description="Código del equipo a reservar")
    fecha: str = Fecha
    hora_inicio: str = Hora
    hora_fin: str = Hora


class CancelarReservaIn(_In):
    reserva_id: str = Field(pattern=r"^[Rr][Ee][Ss]-\d{1,6}$", description="Código de la reserva, p. ej. RES-0001")
    motivo: Optional[str] = Field(default=None, max_length=200)


class RegistrarPrestamoIn(_In):
    usuario_id: str = Field(min_length=3, max_length=20, description="Código del estudiante que retira el equipo")
    equipo_id: str = Field(pattern=r"^[A-Za-z]{3}-\d{2}$")
    reserva_id: Optional[str] = Field(default=None, pattern=r"^[Rr][Ee][Ss]-\d{1,6}$")
    observaciones: Optional[str] = Field(default=None, max_length=300)


class RegistrarDevolucionIn(_In):
    prestamo_id: str = Field(pattern=r"^[Pp][Rr][Ee]-\d{1,6}$", description="Código del préstamo, p. ej. PRE-0001")
    estado: Literal["bueno", "con_observaciones", "danado"]
    observaciones: Optional[str] = Field(default=None, max_length=300)


class SinParametrosIn(_In):
    pass


class ConsultarAuditoriaIn(_In):
    solicitud_id: Optional[str] = Field(default=None, max_length=64)
    sesion_id: Optional[str] = Field(default=None, max_length=64)
    usuario_id: Optional[str] = Field(default=None, max_length=20)
    tipo_evento: Optional[str] = Field(default=None, max_length=40)
    limite: int = Field(default=20, ge=1, le=100)


class ErrorInfo(BaseModel):
    code: str
    message: str
    articulo: Optional[str] = None
    category: Optional[str] = None


class ToolResult(BaseModel):
    """Salida normalizada de toda herramienta (local o MCP)."""
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "error", "denegado", "requiere_autenticacion", "requiere_confirmacion",
                    "servicio_no_disponible", "entrada_invalida"]
    data: Optional[dict[str, Any]] = None
    error: Optional[ErrorInfo] = None
    mensaje: Optional[str] = None


# --- Salida estructurada del router ------------------------------------------
Route = Literal[
    "consulta_reglamento", "consultar_disponibilidad", "crear_reserva", "cancelar_reserva",
    "mis_reservas", "registrar_prestamo", "registrar_devolucion", "auditoria", "autenticacion", "general",
]


class RouteDecision(BaseModel):
    ruta: Route
    confianza: float = Field(ge=0, le=1, default=0.5)


class Slots(BaseModel):
    """Parámetros extraídos del mensaje por el router."""
    tipo_equipo: Optional[TipoEquipo] = None
    equipo_id: Optional[str] = None
    fecha: Optional[str] = None
    hora_inicio: Optional[str] = None
    hora_fin: Optional[str] = None
    reserva_id: Optional[str] = None
    prestamo_id: Optional[str] = None
    usuario_id: Optional[str] = None
    estado: Optional[Literal["bueno", "con_observaciones", "danado"]] = None
    solicitud_id: Optional[str] = None
    alternativa: bool = False
    integridad: bool = False
