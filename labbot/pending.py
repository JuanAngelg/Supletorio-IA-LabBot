"""Operaciones pendientes de confirmación del usuario.

Una herramienta de escritura nunca se ejecuta en el mismo turno en que el LLM la pide:
el orquestador valida, crea una operación pendiente con su clave de idempotencia y
espera la confirmación explícita del usuario (botón o mensaje "confirmo").
"""
from __future__ import annotations

import json
import secrets
import time

from . import config, db


def _canonical(args: dict) -> str:
    return json.dumps({k: v for k, v in sorted(args.items()) if v is not None}, sort_keys=True, ensure_ascii=False)


def expire_stale(session_id: str) -> None:
    with db.app() as conn:
        conn.execute("UPDATE pending_actions SET status = 'expirada' WHERE session_id = ? AND status = 'pendiente' "
                     "AND expires_at < ?", (session_id, time.time()))


def find_same(session_id: str, tool: str, args: dict) -> dict | None:
    """Evita duplicar la operación pendiente si el LLM repite la misma llamada."""
    with db.app() as conn:
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE session_id = ? AND tool = ? AND args_json = ? AND status = 'pendiente' "
            "AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (session_id, tool, _canonical(args), time.time()),
        ).fetchone()
    return _to_dict(row) if row else None


def create(session_id: str, request_id: str, tool: str, args: dict, summary: str) -> dict:
    pid = "op_" + secrets.token_hex(6)
    key = f"idem_{session_id}_{pid}"
    now = time.time()
    with db.app() as conn:
        conn.execute(
            "INSERT INTO pending_actions(id, session_id, request_id, tool, args_json, summary, idempotency_key, status, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?,?, 'pendiente', ?, ?)",
            (pid, session_id, request_id, tool, _canonical(args), summary, key, now, now + config.pending_ttl_seconds()),
        )
    return get(pid)


def get(pid: str) -> dict | None:
    with db.app() as conn:
        row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (pid,)).fetchone()
    return _to_dict(row) if row else None


def latest_open(session_id: str) -> dict | None:
    expire_stale(session_id)
    with db.app() as conn:
        row = conn.execute("SELECT * FROM pending_actions WHERE session_id = ? AND status = 'pendiente' "
                           "ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
    return _to_dict(row) if row else None


def set_status(pid: str, status: str, result: dict | None = None) -> None:
    with db.app() as conn:
        conn.execute("UPDATE pending_actions SET status = ?, result_json = ? WHERE id = ?",
                     (status, json.dumps(result, ensure_ascii=False) if result is not None else None, pid))


def _to_dict(row) -> dict:
    d = dict(row)
    d["args"] = json.loads(d.pop("args_json"))
    d["result"] = json.loads(d["result_json"]) if d.get("result_json") else None
    d.pop("result_json", None)
    return d
