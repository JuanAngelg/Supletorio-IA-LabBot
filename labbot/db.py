"""Bases de datos relacionales (SQLite).

- labbot.db  (operaciones): usuarios, equipos, capacitaciones, sanciones, reservas,
               préstamos e idempotencia. Es la que "cae" en el escenario D.
- app.db     (estado de la aplicación): sesiones, historial de mensajes,
               confirmaciones pendientes y evidencias de autenticación ya usadas.

Las escrituras usan BEGIN IMMEDIATE: SQLite toma el bloqueo de escritura al inicio de la
transacción, así dos reservas concurrentes se serializan y la segunda ve la primera.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config, faults
from .errors import ServiceUnavailable

BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('estudiante','encargado','auditor')),
    active INTEGER NOT NULL DEFAULT 1,
    totp_secret TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equipment (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('disponible','mantenimiento','baja')),
    location TEXT NOT NULL,
    requires_training INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trainings (
    user_id TEXT NOT NULL REFERENCES users(id),
    equipment_type TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY (user_id, equipment_type)
);
CREATE TABLE IF NOT EXISTS sanctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    until_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL REFERENCES equipment(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('activa','cancelada','convertida')),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    cancelled_at TEXT,
    cancelled_by TEXT,
    idempotency_key TEXT UNIQUE,
    CHECK (end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS idx_res_equipment ON reservations(equipment_id, status, start_ts);
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL REFERENCES equipment(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    reservation_id INTEGER REFERENCES reservations(id),
    registered_by TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    due_ts TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('activo','devuelto')),
    notes_out TEXT,
    returned_at TEXT,
    return_registered_by TEXT,
    return_condition TEXT,
    notes_in TEXT,
    idempotency_key TEXT UNIQUE
);
-- Un equipo no puede tener dos préstamos activos a la vez.
CREATE UNIQUE INDEX IF NOT EXISTS uq_loan_active ON loans(equipment_id) WHERE status = 'activo';
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    user_id TEXT NOT NULL,
    user_name TEXT NOT NULL,
    role TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    auth_method TEXT,
    auth_jti TEXT,
    auth_expires_at REAL,
    face_verification_id TEXT,
    fallback_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    request_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    meta_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    request_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    summary TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pendiente','rechazada','expirada','ejecutada','fallida','incierta')),
    result_json TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS used_evidence (
    jti TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    used_at REAL NOT NULL
);
"""


def app_db_path() -> Path:
    return Path(config.env("LABBOT_APP_DB", str(config.var_dir() / "app.db")))


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def connect() -> sqlite3.Connection:
    """Conexión a la BD de operaciones; respeta el fallo inyectado db_down."""
    if faults.active("db_down"):
        raise ServiceUnavailable("base_de_datos", "la base de datos de operaciones no responde")
    try:
        return _open(config.db_path())
    except sqlite3.Error as exc:  # pragma: no cover - depende del entorno
        raise ServiceUnavailable("base_de_datos", str(exc)) from exc


def connect_app() -> sqlite3.Connection:
    return _open(app_db_path())


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True):
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def business():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def app():
    conn = connect_app()
    try:
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    with _open(config.db_path()) as conn:
        conn.executescript(BUSINESS_SCHEMA)
    with _open(app_db_path()) as conn:
        conn.executescript(APP_SCHEMA)


def load_seed() -> dict:
    return json.loads(config.seed_path().read_text(encoding="utf-8"))


def reset_business_data(seed: dict | None = None) -> None:
    """Restablece los datos de operaciones al estado semilla (pruebas y evaluación)."""
    seed = seed or load_seed()
    conn = _open(config.db_path())
    try:
        conn.executescript(BUSINESS_SCHEMA)
        with transaction(conn):
            for table in ("idempotency", "loans", "reservations", "sanctions", "trainings", "equipment", "users"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('reservations','loans','sanctions')")
            for u in seed["users"]:
                conn.execute(
                    "INSERT INTO users(id,name,email,role,active,totp_secret) VALUES (?,?,?,?,?,?)",
                    (u["id"], u["name"], u["email"], u["role"], int(u.get("active", True)), u["totp_secret"]),
                )
            for e in seed["equipment"]:
                conn.execute(
                    "INSERT INTO equipment(id,name,type,status,location,requires_training) VALUES (?,?,?,?,?,?)",
                    (e["id"], e["name"], e["type"], e["status"], e["location"], int(e["requires_training"])),
                )
            for t in seed["trainings"]:
                conn.execute(
                    "INSERT INTO trainings(user_id,equipment_type,approved_at) VALUES (?,?,?)",
                    (t["user_id"], t["equipment_type"], t["approved_at"]),
                )
            for s in seed["sanctions"]:
                conn.execute(
                    "INSERT INTO sanctions(user_id,reason,until_ts) VALUES (?,?,?)",
                    (s["user_id"], s["reason"], s["until_ts"]),
                )
    finally:
        conn.close()


def reset_app_data() -> None:
    conn = _open(app_db_path())
    try:
        conn.executescript(APP_SCHEMA)
        with transaction(conn):
            for table in ("pending_actions", "messages", "used_evidence", "sessions"):
                conn.execute(f"DELETE FROM {table}")
    finally:
        conn.close()
