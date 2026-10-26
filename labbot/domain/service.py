"""Servicio de dominio: reglas de negocio de reservas, préstamos y devoluciones.

Cada regla cita el artículo del reglamento que la origina (REG-LAB-001), de modo que
los rechazos se pueden explicar y auditar. Las operaciones de escritura:
  1. vuelven a validar permisos (policy.require) aunque el orquestador ya lo hizo;
  2. se ejecutan dentro de BEGIN IMMEDIATE (serializa reservas concurrentes);
  3. son idempotentes: una misma clave de idempotencia devuelve el mismo resultado
     y nunca crea un segundo registro.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

from .. import config, db
from ..errors import Conflict, DomainError, Forbidden, NotFound
from .policy import Actor, require

FMT = "%Y-%m-%dT%H:%M"

ART_REQUISITOS = "REG-LAB-001#art3"
ART_RESERVAS = "REG-LAB-001#art4"
ART_CANCELACION = "REG-LAB-001#art5"
ART_PRESTAMO = "REG-LAB-001#art6"
ART_DEVOLUCION = "REG-LAB-001#art7"
ART_SANCIONES = "REG-LAB-001#art8"
ART_MANTENIMIENTO = "REG-LAB-001#art9"

# Horario de atención (Art. 4): lunes a viernes 07:00-20:00, sábado 08:00-13:00.
LAB_HOURS = {0: (7, 20), 1: (7, 20), 2: (7, 20), 3: (7, 20), 4: (7, 20), 5: (8, 13)}
MIN_DURATION = timedelta(minutes=30)
MAX_DURATION = timedelta(hours=4)
MAX_ADVANCE = timedelta(days=14)
MAX_ACTIVE_RESERVATIONS = 2
LOAN_EARLY_PICKUP = timedelta(minutes=15)
IMMEDIATE_LOAN = timedelta(hours=2)
LATE_TOLERANCE = timedelta(minutes=30)
LATE_SANCTION = timedelta(days=7)
DAMAGE_SANCTION = timedelta(days=30)

RETURN_STATES = ("bueno", "con_observaciones", "danado")


# --- utilidades -------------------------------------------------------------
def fmt(dt: datetime) -> str:
    return dt.strftime(FMT)


def parse_slot(fecha: str, hora: str) -> datetime:
    try:
        return datetime.strptime(f"{fecha}T{hora}", FMT)
    except ValueError as exc:
        raise DomainError("FECHA_INVALIDA", f"Fecha u hora inválida: {fecha} {hora}") from exc


def res_code(rid: int) -> str:
    return f"RES-{rid:04d}"


def loan_code(lid: int) -> str:
    return f"PRE-{lid:04d}"


def parse_code(code: str, prefix: str) -> int:
    try:
        head, num = code.strip().upper().split("-", 1)
        if head != prefix:
            raise ValueError
        return int(num)
    except (ValueError, AttributeError) as exc:
        raise NotFound(f"Identificador inválido: {code}") from exc


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# --- consultas --------------------------------------------------------------
def get_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise NotFound(f"No existe el usuario {user_id}.", code="USUARIO_NO_EXISTE")
    return row


def get_equipment(conn: sqlite3.Connection, equipment_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id.upper(),)).fetchone()
    if not row:
        raise NotFound(f"No existe el equipo {equipment_id}.", code="EQUIPO_NO_EXISTE")
    return row


def list_users() -> list[dict]:
    with db.business() as conn:
        rows = conn.execute("SELECT id, name, email, role FROM users WHERE active = 1 ORDER BY "
                            "CASE role WHEN 'estudiante' THEN 0 WHEN 'encargado' THEN 1 ELSE 2 END, id").fetchall()
    return [dict(r) for r in rows]


def _conflicts(conn, equipment_id: str, start: datetime, end: datetime, exclude_reservation: int | None = None) -> list[dict]:
    items = []
    for r in conn.execute(
        "SELECT id, start_ts, end_ts FROM reservations WHERE equipment_id = ? AND status = 'activa' "
        "AND start_ts < ? AND end_ts > ?",
        (equipment_id, fmt(end), fmt(start)),
    ):
        if r["id"] != exclude_reservation:
            items.append({"tipo": "reserva", "inicio": r["start_ts"], "fin": r["end_ts"]})
    for l in conn.execute(
        "SELECT start_ts, due_ts FROM loans WHERE equipment_id = ? AND status = 'activo' AND start_ts < ? AND due_ts > ?",
        (equipment_id, fmt(end), fmt(start)),
    ):
        items.append({"tipo": "prestamo", "inicio": l["start_ts"], "fin": l["due_ts"]})
    return items


def availability(tipo_equipo: str | None, equipo_id: str | None, fecha: str, hora_inicio: str, hora_fin: str) -> dict:
    start, end = parse_slot(fecha, hora_inicio), parse_slot(fecha, hora_fin)
    if end <= start:
        raise DomainError("DURACION_INVALIDA", "La hora de fin debe ser posterior a la de inicio.", ART_RESERVAS)
    with db.business() as conn:
        if equipo_id:
            rows = [get_equipment(conn, equipo_id)]
        elif tipo_equipo:
            rows = conn.execute("SELECT * FROM equipment WHERE type = ? ORDER BY id", (tipo_equipo,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM equipment ORDER BY id").fetchall()
        result = []
        for e in rows:
            conflicts = _conflicts(conn, e["id"], start, end)
            if e["status"] != "disponible":
                motivo = f"Equipo en estado '{e['status']}' ({ART_MANTENIMIENTO})."
            elif conflicts:
                motivo = "Ocupado en ese horario."
            else:
                motivo = None
            result.append({
                "equipo_id": e["id"], "nombre": e["name"], "tipo": e["type"], "ubicacion": e["location"],
                "estado": e["status"], "requiere_capacitacion": bool(e["requires_training"]),
                "disponible": motivo is None, "motivo": motivo,
                # No se revela quién ocupa el equipo: solo el intervalo.
                "ocupado_en": [{"inicio": c["inicio"], "fin": c["fin"]} for c in conflicts],
            })
    return {"fecha": fecha, "hora_inicio": hora_inicio, "hora_fin": hora_fin, "equipos": result}


def list_user_items(actor: Actor, target_user: str | None = None, include_history: bool = False) -> dict:
    target = target_user or actor.user_id
    require(actor, "consultar_mis_reservas_prestamos", {"usuario_id": target})
    status_res = "" if include_history else "AND r.status = 'activa'"
    status_loan = "" if include_history else "AND l.status = 'activo'"
    with db.business() as conn:
        user = get_user(conn, target)
        res = conn.execute(
            f"SELECT r.*, e.name AS equipment_name FROM reservations r JOIN equipment e ON e.id = r.equipment_id "
            f"WHERE r.user_id = ? {status_res} ORDER BY r.start_ts",
            (target,),
        ).fetchall()
        loans = conn.execute(
            f"SELECT l.*, e.name AS equipment_name FROM loans l JOIN equipment e ON e.id = l.equipment_id "
            f"WHERE l.user_id = ? {status_loan} ORDER BY l.start_ts",
            (target,),
        ).fetchall()
    return {
        "usuario_id": target, "nombre": user["name"],
        "reservas": [{
            "reserva_id": res_code(r["id"]), "equipo_id": r["equipment_id"], "equipo": r["equipment_name"],
            "inicio": r["start_ts"], "fin": r["end_ts"], "estado": r["status"],
        } for r in res],
        "prestamos": [{
            "prestamo_id": loan_code(l["id"]), "equipo_id": l["equipment_id"], "equipo": l["equipment_name"],
            "inicio": l["start_ts"], "vence": l["due_ts"], "estado": l["status"],
            "devuelto": l["returned_at"], "condicion_devolucion": l["return_condition"],
        } for l in loans],
    }


# --- validaciones de reglas ---------------------------------------------------
def _check_window(start: datetime, end: datetime, now: datetime) -> None:
    if start <= now:
        raise DomainError("FECHA_PASADA", "La reserva debe empezar en el futuro.", ART_RESERVAS)
    if end - start < MIN_DURATION or end - start > MAX_DURATION:
        raise DomainError("DURACION_INVALIDA", "La reserva debe durar entre 30 minutos y 4 horas.", ART_RESERVAS)
    if start - now > MAX_ADVANCE:
        raise DomainError("ANTICIPACION_EXCEDIDA", "Solo se puede reservar con hasta 14 días de anticipación.", ART_RESERVAS)
    hours = LAB_HOURS.get(start.weekday())
    if start.date() != end.date() or hours is None or start.hour < hours[0] or (end.hour, end.minute) > (hours[1], 0):
        raise DomainError(
            "HORARIO_FUERA_DE_ATENCION",
            "Fuera del horario de atención (lunes a viernes 07:00-20:00, sábados 08:00-13:00).",
            ART_RESERVAS,
        )


def _check_eligibility(conn, user: sqlite3.Row, equipment: sqlite3.Row, now: datetime) -> None:
    if not user["active"]:
        raise Forbidden("La cuenta del usuario no está activa.", code="CUENTA_INACTIVA", articulo=ART_REQUISITOS)
    sanction = conn.execute(
        "SELECT reason, until_ts FROM sanctions WHERE user_id = ? AND until_ts > ? ORDER BY until_ts DESC",
        (user["id"], fmt(now)),
    ).fetchone()
    if sanction:
        raise DomainError("SANCION_VIGENTE", f"El usuario tiene una sanción vigente hasta {sanction['until_ts']}.", ART_SANCIONES)
    overdue = conn.execute(
        "SELECT id FROM loans WHERE user_id = ? AND status = 'activo' AND due_ts < ?", (user["id"], fmt(now))
    ).fetchone()
    if overdue:
        raise DomainError("PRESTAMO_VENCIDO", f"El usuario tiene el préstamo {loan_code(overdue['id'])} vencido.", ART_REQUISITOS)
    if equipment["requires_training"]:
        trained = conn.execute(
            "SELECT 1 FROM trainings WHERE user_id = ? AND equipment_type = ?", (user["id"], equipment["type"])
        ).fetchone()
        if not trained:
            raise DomainError(
                "CAPACITACION_REQUERIDA",
                f"Se requiere la inducción aprobada para equipos de tipo '{equipment['type']}'.",
                ART_REQUISITOS,
            )


def _check_equipment_usable(equipment: sqlite3.Row) -> None:
    if equipment["status"] != "disponible":
        raise DomainError(
            "EQUIPO_NO_DISPONIBLE", f"El equipo {equipment['id']} está en estado '{equipment['status']}'.", ART_MANTENIMIENTO
        )


def validate_reservation(conn, actor: Actor, equipo_id: str, start: datetime, end: datetime) -> dict:
    require(actor, "crear_reserva")
    now = config.now()
    _check_window(start, end, now)
    equipment = get_equipment(conn, equipo_id)
    _check_equipment_usable(equipment)
    user = get_user(conn, actor.user_id)
    _check_eligibility(conn, user, equipment, now)
    if actor.role == "estudiante":
        active = conn.execute(
            "SELECT COUNT(*) FROM reservations WHERE user_id = ? AND status = 'activa' AND end_ts > ?",
            (actor.user_id, fmt(now)),
        ).fetchone()[0]
        if active >= MAX_ACTIVE_RESERVATIONS:
            raise DomainError("LIMITE_RESERVAS", "Ya tiene 2 reservas activas, el máximo permitido.", ART_RESERVAS)
        overlap = conn.execute(
            "SELECT id FROM reservations WHERE user_id = ? AND status = 'activa' AND start_ts < ? AND end_ts > ?",
            (actor.user_id, fmt(end), fmt(start)),
        ).fetchone()
        if overlap:
            raise Conflict(f"Ya tiene la reserva {res_code(overlap['id'])} en ese mismo horario.",
                           code="RESERVA_SUPERPUESTA_USUARIO")
    if _conflicts(conn, equipment["id"], start, end):
        raise Conflict(f"El equipo {equipment['id']} ya está reservado o prestado en ese horario.")
    return {
        "equipo_id": equipment["id"], "equipo": equipment["name"], "usuario_id": user["id"], "usuario": user["name"],
        "inicio": fmt(start), "fin": fmt(end),
    }


def _load_reservation(conn, reserva_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (parse_code(reserva_id, "RES"),)).fetchone()
    if not row:
        raise NotFound(f"No existe la reserva {reserva_id}.", code="RESERVA_NO_EXISTE")
    return row


def validate_cancellation(conn, actor: Actor, reserva_id: str) -> dict:
    require(actor, "cancelar_reserva")
    res = _load_reservation(conn, reserva_id)
    if res["user_id"] != actor.user_id and actor.role != "encargado":
        raise Forbidden("No puede cancelar reservas de otra persona.", code="RESERVA_DE_OTRO", articulo=ART_CANCELACION)
    if res["status"] != "activa":
        raise DomainError("RESERVA_NO_CANCELABLE", f"La reserva está en estado '{res['status']}'.", ART_CANCELACION)
    if datetime.strptime(res["start_ts"], FMT) <= config.now():
        raise DomainError("RESERVA_NO_CANCELABLE", "Solo se puede cancelar antes de la hora de inicio.", ART_CANCELACION)
    equipment = get_equipment(conn, res["equipment_id"])
    return {
        "reserva_id": res_code(res["id"]), "equipo_id": res["equipment_id"], "equipo": equipment["name"],
        "inicio": res["start_ts"], "fin": res["end_ts"], "titular": res["user_id"],
    }


def validate_loan(conn, actor: Actor, usuario_id: str, equipo_id: str, reserva_id: str | None) -> dict:
    require(actor, "registrar_prestamo")
    now = config.now()
    borrower = get_user(conn, usuario_id)
    equipment = get_equipment(conn, equipo_id)
    _check_equipment_usable(equipment)
    if conn.execute("SELECT 1 FROM loans WHERE equipment_id = ? AND status = 'activo'", (equipment["id"],)).fetchone():
        raise Conflict(f"El equipo {equipment['id']} ya está prestado.", code="EQUIPO_PRESTADO", articulo=ART_PRESTAMO)
    _check_eligibility(conn, borrower, equipment, now)
    hours = LAB_HOURS.get(now.weekday())
    if hours is None or not (hours[0] <= now.hour < hours[1]):
        raise DomainError("HORARIO_FUERA_DE_ATENCION", "Los préstamos solo se registran en horario de atención.", ART_PRESTAMO)

    reservation = None
    if reserva_id:
        reservation = _load_reservation(conn, reserva_id)
        if reservation["user_id"] != borrower["id"] or reservation["equipment_id"] != equipment["id"]:
            raise DomainError("RESERVA_NO_CORRESPONDE", "La reserva no corresponde a ese usuario y equipo.", ART_PRESTAMO)
    else:
        reservation = conn.execute(
            "SELECT * FROM reservations WHERE user_id = ? AND equipment_id = ? AND status = 'activa' "
            "AND start_ts <= ? AND end_ts > ? ORDER BY start_ts LIMIT 1",
            (borrower["id"], equipment["id"], fmt(now + LOAN_EARLY_PICKUP), fmt(now)),
        ).fetchone()

    if reservation is not None:
        start = datetime.strptime(reservation["start_ts"], FMT)
        end = datetime.strptime(reservation["end_ts"], FMT)
        if reservation["status"] != "activa" or not (start - LOAN_EARLY_PICKUP <= now < end):
            raise DomainError("RESERVA_NO_VIGENTE", "La reserva no está vigente en este momento.", ART_PRESTAMO)
        due = end
    else:
        closing = now.replace(hour=hours[1], minute=0)
        due = min(now + IMMEDIATE_LOAN, closing)
        if _conflicts(conn, equipment["id"], now, due):
            raise Conflict(
                f"El equipo {equipment['id']} tiene una reserva de otra persona en las próximas horas.",
                code="EQUIPO_RESERVADO", articulo=ART_PRESTAMO,
            )
    return {
        "usuario_id": borrower["id"], "usuario": borrower["name"], "equipo_id": equipment["id"], "equipo": equipment["name"],
        "reserva_id": res_code(reservation["id"]) if reservation is not None else None,
        "inicio": fmt(now), "vence": fmt(due),
    }


def _load_loan(conn, prestamo_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM loans WHERE id = ?", (parse_code(prestamo_id, "PRE"),)).fetchone()
    if not row:
        raise NotFound(f"No existe el préstamo {prestamo_id}.", code="PRESTAMO_NO_EXISTE")
    return row


def validate_return(conn, actor: Actor, prestamo_id: str, estado: str) -> dict:
    require(actor, "registrar_devolucion")
    if estado not in RETURN_STATES:
        raise DomainError("ESTADO_INVALIDO", f"Estado de devolución inválido: {estado}.", ART_DEVOLUCION)
    loan = _load_loan(conn, prestamo_id)
    if loan["status"] != "activo":
        raise DomainError("PRESTAMO_NO_ACTIVO", "El préstamo ya fue devuelto.", ART_DEVOLUCION)
    now = config.now()
    late = now > datetime.strptime(loan["due_ts"], FMT) + LATE_TOLERANCE
    return {
        "prestamo_id": loan_code(loan["id"]), "equipo_id": loan["equipment_id"], "usuario_id": loan["user_id"],
        "estado_equipo": estado, "devolucion_tardia": late, "vence": loan["due_ts"], "fecha_devolucion": fmt(now),
    }


# --- escrituras idempotentes ---------------------------------------------------
def _idempotent(conn, key: str, operation: str, actor: Actor, payload: dict, fn) -> dict:
    request_hash = hashlib.sha256(_canonical([operation, actor.user_id, payload]).encode()).hexdigest()
    row = conn.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()
    if row:
        if row["request_hash"] != request_hash:
            raise DomainError("IDEMPOTENCIA_CONFLICTO", "La clave de idempotencia ya se usó con otros datos.")
        result = json.loads(row["result_json"])
        result["repetida"] = True  # reintento: se devuelve el resultado original sin duplicar
        return result
    result = fn()
    conn.execute(
        "INSERT INTO idempotency(key, operation, actor_id, request_hash, result_json, created_at) VALUES (?,?,?,?,?,?)",
        (key, operation, actor.user_id, request_hash, _canonical(result), fmt(config.now())),
    )
    result = dict(result)
    result["repetida"] = False
    return result


def create_reservation(actor: Actor, equipo_id: str, fecha: str, hora_inicio: str, hora_fin: str, idempotency_key: str) -> dict:
    start, end = parse_slot(fecha, hora_inicio), parse_slot(fecha, hora_fin)
    payload = {"equipo_id": equipo_id.upper(), "inicio": fmt(start), "fin": fmt(end)}
    with db.business() as conn, db.transaction(conn):
        def run():
            preview = validate_reservation(conn, actor, equipo_id, start, end)
            cur = conn.execute(
                "INSERT INTO reservations(equipment_id,user_id,start_ts,end_ts,status,created_at,created_by,idempotency_key) "
                "VALUES (?,?,?,?, 'activa', ?, ?, ?)",
                (preview["equipo_id"], actor.user_id, preview["inicio"], preview["fin"], fmt(config.now()), actor.user_id, idempotency_key),
            )
            return {**preview, "reserva_id": res_code(cur.lastrowid), "estado": "activa",
                    "cambios": [{"tabla": "reservations", "id": cur.lastrowid, "operacion": "INSERT"}]}
        return _idempotent(conn, idempotency_key, "crear_reserva", actor, payload, run)


def cancel_reservation(actor: Actor, reserva_id: str, idempotency_key: str, motivo: str | None = None) -> dict:
    with db.business() as conn, db.transaction(conn):
        def run():
            preview = validate_cancellation(conn, actor, reserva_id)
            rid = parse_code(reserva_id, "RES")
            conn.execute(
                "UPDATE reservations SET status = 'cancelada', cancelled_at = ?, cancelled_by = ? WHERE id = ? AND status = 'activa'",
                (fmt(config.now()), actor.user_id, rid),
            )
            return {**preview, "estado": "cancelada", "motivo": motivo,
                    "cambios": [{"tabla": "reservations", "id": rid, "operacion": "UPDATE", "campo": "status",
                                 "antes": "activa", "despues": "cancelada"}]}
        return _idempotent(conn, idempotency_key, "cancelar_reserva", actor, {"reserva_id": reserva_id.upper()}, run)


def register_loan(actor: Actor, usuario_id: str, equipo_id: str, idempotency_key: str,
                  reserva_id: str | None = None, observaciones: str | None = None) -> dict:
    payload = {"usuario_id": usuario_id, "equipo_id": equipo_id.upper(), "reserva_id": reserva_id}
    with db.business() as conn, db.transaction(conn):
        def run():
            preview = validate_loan(conn, actor, usuario_id, equipo_id, reserva_id)
            rid = parse_code(preview["reserva_id"], "RES") if preview["reserva_id"] else None
            cur = conn.execute(
                "INSERT INTO loans(equipment_id,user_id,reservation_id,registered_by,start_ts,due_ts,status,notes_out,idempotency_key) "
                "VALUES (?,?,?,?,?,?, 'activo', ?, ?)",
                (preview["equipo_id"], preview["usuario_id"], rid, actor.user_id, preview["inicio"], preview["vence"],
                 observaciones, idempotency_key),
            )
            changes = [{"tabla": "loans", "id": cur.lastrowid, "operacion": "INSERT"}]
            if rid:
                conn.execute("UPDATE reservations SET status = 'convertida' WHERE id = ?", (rid,))
                changes.append({"tabla": "reservations", "id": rid, "operacion": "UPDATE", "campo": "status",
                                "antes": "activa", "despues": "convertida"})
            return {**preview, "prestamo_id": loan_code(cur.lastrowid), "estado": "activo", "registrado_por": actor.user_id,
                    "cambios": changes}
        return _idempotent(conn, idempotency_key, "registrar_prestamo", actor, payload, run)


def register_return(actor: Actor, prestamo_id: str, estado: str, idempotency_key: str, observaciones: str | None = None) -> dict:
    with db.business() as conn, db.transaction(conn):
        def run():
            preview = validate_return(conn, actor, prestamo_id, estado)
            lid = parse_code(prestamo_id, "PRE")
            now = config.now()
            conn.execute(
                "UPDATE loans SET status = 'devuelto', returned_at = ?, return_registered_by = ?, return_condition = ?, notes_in = ? "
                "WHERE id = ? AND status = 'activo'",
                (fmt(now), actor.user_id, estado, observaciones, lid),
            )
            changes = [{"tabla": "loans", "id": lid, "operacion": "UPDATE", "campo": "status", "antes": "activo", "despues": "devuelto"}]
            sanciones = []
            if estado == "danado":
                conn.execute("UPDATE equipment SET status = 'mantenimiento' WHERE id = ?", (preview["equipo_id"],))
                changes.append({"tabla": "equipment", "id": preview["equipo_id"], "operacion": "UPDATE", "campo": "status",
                                "antes": "disponible", "despues": "mantenimiento"})
            if preview["devolucion_tardia"]:
                until = fmt(now + LATE_SANCTION)
                conn.execute("INSERT INTO sanctions(user_id, reason, until_ts) VALUES (?,?,?)",
                             (preview["usuario_id"], f"Devolución tardía de {preview['equipo_id']} ({ART_SANCIONES})", until))
                sanciones.append({"motivo": "devolucion_tardia", "hasta": until, "articulo": ART_SANCIONES})
                changes.append({"tabla": "sanctions", "operacion": "INSERT"})
            return {**preview, "estado": "devuelto", "sanciones": sanciones, "registrado_por": actor.user_id, "cambios": changes}
        return _idempotent(conn, idempotency_key, "registrar_devolucion", actor, {"prestamo_id": prestamo_id.upper(), "estado": estado}, run)


def preview_operation(actor: Actor, tool: str, args: dict) -> dict:
    """Valida una operación de escritura sin ejecutarla (para el resumen de confirmación)."""
    with db.business() as conn:
        if tool == "crear_reserva":
            start = parse_slot(args["fecha"], args["hora_inicio"])
            end = parse_slot(args["fecha"], args["hora_fin"])
            return validate_reservation(conn, actor, args["equipo_id"], start, end)
        if tool == "cancelar_reserva":
            return validate_cancellation(conn, actor, args["reserva_id"])
        if tool == "registrar_prestamo":
            return validate_loan(conn, actor, args["usuario_id"], args["equipo_id"], args.get("reserva_id"))
        if tool == "registrar_devolucion":
            return validate_return(conn, actor, args["prestamo_id"], args["estado"])
    raise DomainError("OPERACION_DESCONOCIDA", f"Operación desconocida: {tool}")


def execute_operation(actor: Actor, tool: str, args: dict, idempotency_key: str) -> dict:
    if tool == "crear_reserva":
        return create_reservation(actor, args["equipo_id"], args["fecha"], args["hora_inicio"], args["hora_fin"], idempotency_key)
    if tool == "cancelar_reserva":
        return cancel_reservation(actor, args["reserva_id"], idempotency_key, args.get("motivo"))
    if tool == "registrar_prestamo":
        return register_loan(actor, args["usuario_id"], args["equipo_id"], idempotency_key, args.get("reserva_id"), args.get("observaciones"))
    if tool == "registrar_devolucion":
        return register_return(actor, args["prestamo_id"], args["estado"], idempotency_key, args.get("observaciones"))
    raise DomainError("OPERACION_DESCONOCIDA", f"Operación desconocida: {tool}")


def lookup_operation(idempotency_key: str) -> dict | None:
    """Reconciliación: ¿se llegó a ejecutar la operación con esta clave?"""
    with db.business() as conn:
        row = conn.execute("SELECT result_json FROM idempotency WHERE key = ?", (idempotency_key,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def snapshot() -> dict:
    """Estado completo de las tablas de operación (panel de demostración y evaluación)."""
    with db.business() as conn:
        return {
            "equipos": [dict(r) for r in conn.execute("SELECT id, name, type, status FROM equipment ORDER BY id")],
            "reservas": [{**dict(r), "codigo": res_code(r["id"])} for r in conn.execute(
                "SELECT id, equipment_id, user_id, start_ts, end_ts, status, created_at, cancelled_by FROM reservations ORDER BY id")],
            "prestamos": [{**dict(r), "codigo": loan_code(r["id"])} for r in conn.execute(
                "SELECT id, equipment_id, user_id, start_ts, due_ts, status, registered_by, returned_at, return_condition FROM loans ORDER BY id")],
            "sanciones": [dict(r) for r in conn.execute("SELECT user_id, reason, until_ts FROM sanctions ORDER BY id")],
        }
