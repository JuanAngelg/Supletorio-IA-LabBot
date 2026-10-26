"""Gestión de sesiones e historial de conversación (BD app.db)."""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime

from . import audit, db, security
from .domain import service
from .errors import Forbidden, NotFound


@dataclass
class Session:
    id: str
    user_id: str
    user_name: str
    role: str
    auth_method: str | None
    auth_expires_at: float | None
    face_verification_id: str | None

    @property
    def authenticated(self) -> bool:
        return bool(self.auth_method and self.auth_expires_at and self.auth_expires_at > time.time())

    def auth_state(self) -> dict:
        return {
            "autenticado": self.authenticated,
            "metodo": self.auth_method if self.authenticated else None,
            "vence_en_s": int(self.auth_expires_at - time.time()) if self.authenticated else 0,
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row_to_session(row) -> Session:
    return Session(row["id"], row["user_id"], row["user_name"], row["role"], row["auth_method"],
                   row["auth_expires_at"], row["face_verification_id"])


def create_session(user_id: str) -> tuple[Session, str]:
    """La persona *declara* su identidad; queda sin autenticar hasta verificar el rostro."""
    with db.business() as conn:
        user = service.get_user(conn, user_id)
    sid = "ses_" + secrets.token_hex(8)
    token = security.new_session_token()
    with db.app() as conn:
        conn.execute(
            "INSERT INTO sessions(id, token_hash, user_id, user_name, role, title, created_at, last_activity) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, security.hash_token(token), user["id"], user["name"], user["role"], "Nueva conversación", _now(), _now()),
        )
    audit.log("session_created", session_id=sid, user_id=user["id"], rol=user["role"], identidad="declarada")
    return get_session(sid), token


def get_session(session_id: str, token: str | None = None, *, check_token: bool = False) -> Session:
    with db.app() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        raise NotFound("Sesión inexistente", code="SESION_NO_EXISTE")
    if check_token and (not token or not secrets.compare_digest(row["token_hash"], security.hash_token(token))):
        raise Forbidden("Token de sesión inválido", code="SESION_INVALIDA")
    return _row_to_session(row)


def list_sessions(user_id: str) -> list[dict]:
    with db.app() as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.created_at, s.last_activity, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS mensajes "
            "FROM sessions s WHERE s.user_id = ? ORDER BY s.last_activity DESC", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def add_message(session_id: str, role: str, content: str, request_id: str | None = None, meta: dict | None = None) -> None:
    with db.app() as conn:
        conn.execute(
            "INSERT INTO messages(session_id, request_id, role, content, meta_json, created_at) VALUES (?,?,?,?,?,?)",
            (session_id, request_id, role, content, json.dumps(meta or {}, ensure_ascii=False), _now()),
        )
        conn.execute("UPDATE sessions SET last_activity = ? WHERE id = ?", (_now(), session_id))
        if role == "user":
            conn.execute("UPDATE sessions SET title = ? WHERE id = ? AND title = 'Nueva conversación'",
                         (content[:60], session_id))


def history(session_id: str, limit: int = 12) -> list[dict]:
    """Últimos mensajes para dar contexto al LLM (solo texto, sin metadatos sensibles)."""
    with db.app() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? AND role IN ('user','assistant') "
            "ORDER BY id DESC LIMIT ?", (session_id, limit)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def messages(session_id: str) -> list[dict]:
    with db.app() as conn:
        rows = conn.execute("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    return [{"role": r["role"], "content": r["content"], "request_id": r["request_id"],
             "meta": json.loads(r["meta_json"] or "{}"), "created_at": r["created_at"]} for r in rows]


def bind_auth(session_id: str, method: str, jti: str, expires_at: float) -> None:
    with db.app() as conn:
        conn.execute("INSERT INTO used_evidence(jti, session_id, used_at) VALUES (?,?,?)", (jti, session_id, time.time()))
        conn.execute("UPDATE sessions SET auth_method = ?, auth_jti = ?, auth_expires_at = ?, fallback_failures = 0 WHERE id = ?",
                     (method, jti, expires_at, session_id))


def evidence_used(jti: str) -> bool:
    with db.app() as conn:
        return conn.execute("SELECT 1 FROM used_evidence WHERE jti = ?", (jti,)).fetchone() is not None


def set_face_verification(session_id: str, vid: str | None) -> None:
    with db.app() as conn:
        conn.execute("UPDATE sessions SET face_verification_id = ? WHERE id = ?", (vid, session_id))


def register_fallback_failure(session_id: str) -> int:
    with db.app() as conn:
        conn.execute("UPDATE sessions SET fallback_failures = fallback_failures + 1 WHERE id = ?", (session_id,))
        return conn.execute("SELECT fallback_failures FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]


def fallback_failures(session_id: str) -> int:
    with db.app() as conn:
        return conn.execute("SELECT fallback_failures FROM sessions WHERE id = ?", (session_id,)).fetchone()[0]
