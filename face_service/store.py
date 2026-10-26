"""Almacenamiento del servicio facial (BD propia: var/face.db).

- Plantillas: vector de 128 floats cifrado con Fernet (AES-128-CBC + HMAC-SHA256) con una
  clave que solo lee este servicio (var/keys/face_template.key o FACE_TEMPLATE_KEY).
- Nunca se guardan imágenes: se procesan en memoria y se descartan.
- Consentimiento: registro de otorgamiento/revocación con versión y fecha (sin biometría).
- Eliminación: DELETE físico + VACUUM para no dejar la plantilla en páginas libres.
- Verificaciones: desafío, estado, intentos y la evidencia firmada (que se entrega una sola
  vez al backend y luego se borra).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
from cryptography.fernet import Fernet

from labbot import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    user_id TEXT PRIMARY KEY,
    template_enc BLOB NOT NULL,
    engine TEXT NOT NULL,
    consent_version TEXT NOT NULL,
    created_at REAL NOT NULL,
    retention_until REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    consent_version TEXT,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    challenge_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    evidence_token TEXT,
    evidence_jti TEXT
);
"""

RETENTION_SECONDS = 180 * 24 * 3600


def db_path() -> Path:
    return Path(os.environ.get("FACE_DB", str(config.var_dir() / "face.db")))


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _fernet() -> Fernet:
    key = os.environ.get("FACE_TEMPLATE_KEY")
    if not key:
        path = config.keys_dir() / "face_template.key"
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
        key = path.read_bytes().decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def save_template(user_id: str, template: np.ndarray, engine: str, consent_version: str) -> None:
    blob = _fernet().encrypt(template.astype(np.float32).tobytes())
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO templates(user_id, template_enc, engine, consent_version, created_at, retention_until) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, blob, engine, consent_version, now, now + RETENTION_SECONDS),
        )
        conn.execute("INSERT INTO consent_log(user_id, action, consent_version, ts) VALUES (?,?,?,?)",
                     (user_id, "otorgado_y_enrolado", consent_version, now))


def load_template(user_id: str) -> np.ndarray | None:
    with connect() as conn:
        row = conn.execute("SELECT template_enc, retention_until FROM templates WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None
    if row["retention_until"] < time.time():
        delete_template(user_id, reason="vencimiento_retencion")
        return None
    return np.frombuffer(_fernet().decrypt(row["template_enc"]), dtype=np.float32)


def delete_template(user_id: str, reason: str = "revocacion") -> bool:
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM templates WHERE user_id = ?", (user_id,))
        conn.execute("INSERT INTO consent_log(user_id, action, consent_version, ts) VALUES (?,?,?,?)",
                     (user_id, f"eliminado:{reason}", None, time.time()))
        conn.execute("VACUUM")
        return cur.rowcount > 0
    finally:
        conn.close()


def purge_expired() -> int:
    with connect() as conn:
        users = [r["user_id"] for r in conn.execute("SELECT user_id FROM templates WHERE retention_until < ?", (time.time(),))]
    for u in users:
        delete_template(u, reason="vencimiento_retencion")
    return len(users)


def enrollment_status(user_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT engine, consent_version, created_at, retention_until FROM templates WHERE user_id = ?",
                           (user_id,)).fetchone()
    return {"enrolado": bool(row), **({k: row[k] for k in row.keys()} if row else {})}


def create_verification(vid: str, user_id: str, session_id: str, challenge: list[str], ttl: int) -> None:
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO verifications(id, user_id, session_id, challenge_json, created_at, expires_at, status) "
            "VALUES (?,?,?,?,?,?, 'pendiente')",
            (vid, user_id, session_id, json.dumps(challenge), now, now + ttl),
        )


def get_verification(vid: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM verifications WHERE id = ?", (vid,)).fetchone()


def finish_verification(vid: str, status: str, reason: str, token: str | None = None, jti: str | None = None) -> bool:
    """Cierra la verificación una sola vez (las capturas no se pueden reenviar)."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE verifications SET status = ?, reason = ?, evidence_token = ?, evidence_jti = ? "
            "WHERE id = ? AND status = 'pendiente'",
            (status, reason, token, jti, vid),
        )
        return cur.rowcount == 1


def take_evidence(vid: str) -> str | None:
    """Entrega la evidencia al backend una sola vez y la borra del servicio."""
    with connect() as conn:
        row = conn.execute("SELECT evidence_token FROM verifications WHERE id = ?", (vid,)).fetchone()
        if row and row["evidence_token"]:
            conn.execute("UPDATE verifications SET evidence_token = NULL WHERE id = ?", (vid,))
            return row["evidence_token"]
    return None


def recent_failures(user_id: str, window: int = 900) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM verifications WHERE user_id = ? AND status = 'rechazada' AND created_at > ?",
            (user_id, time.time() - window),
        ).fetchone()[0]
