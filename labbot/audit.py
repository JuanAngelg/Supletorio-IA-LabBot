"""Registro de auditoría de solo adición con cadena de hashes (HMAC-SHA256).

Diseño
------
- BD propia (var/audit.db), separada de la de operaciones: si la BD de operaciones cae,
  la auditoría sigue registrando el fallo.
- Solo adición para la aplicación: triggers que abortan cualquier UPDATE o DELETE.
- Cada evento guarda prev_hash y hash = HMAC(clave, prev_hash || JSON canónico del evento).
  Alterar, borrar, insertar o reordenar un evento rompe la cadena desde ese punto.
- Anclajes: al cerrar cada solicitud se copia (seq, hash) a un archivo externo de solo
  adición (simula almacenamiento WORM). Así se detecta el truncamiento del final del log
  y la reescritura completa de la cadena por quien no controle también los anclajes.
- Nunca se registran imágenes, plantillas biométricas, tokens ni secretos: sanitize()
  elimina esas claves antes de escribir.

Límites (ver docs/AUDITORIA.md): quien posea la clave HMAC *y* pueda reescribir la BD
*y* los anclajes puede rehacer la historia; los eventos que nunca se escribieron no se
pueden detectar; y el truncamiento posterior al último anclaje pasa inadvertido.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import config

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    request_id TEXT,
    session_id TEXT,
    user_id TEXT,
    event_type TEXT NOT NULL,
    data_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log es de solo adicion'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log es de solo adicion'); END;
"""

# Claves que jamás deben llegar al registro general (biometría, credenciales, tokens).
SENSITIVE_KEYS = {
    "frames", "frame", "image", "images", "imagen", "imagenes", "template", "plantilla", "embedding",
    "evidence", "evidence_token", "token", "ctx_token", "session_token", "codigo_totp", "totp_code", "totp",
    "totp_secret", "password", "secret", "ticket",
}

# Eventos que toda solicitud del chat debe tener para considerarse completa.
REQUIRED_PER_REQUEST = {"request_received", "final_result"}


def _connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.audit_db_path(), timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _key() -> bytes:
    return config.secret("AUDIT_HMAC_KEY").encode()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def sanitize(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in SENSITIVE_KEYS:
                out[k] = "[omitido]"
            else:
                out[k] = sanitize(v)
        return out
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…[truncado]"
    return value


def compute_hash(prev_hash: str, record: dict, key: bytes | None = None) -> str:
    body = prev_hash + canonical(record)
    return hmac.new(key or _key(), body.encode(), hashlib.sha256).hexdigest()


def _record(ts, request_id, session_id, user_id, event_type, data_json) -> dict:
    return {"ts": ts, "request_id": request_id, "session_id": session_id, "user_id": user_id,
            "event_type": event_type, "data": data_json}


def log(event_type: str, *, request_id: str | None = None, session_id: str | None = None,
        user_id: str | None = None, **data) -> int:
    data_json = canonical(sanitize(data))
    ts = datetime.now().isoformat(timespec="milliseconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            seq = (row["seq"] + 1) if row else 1
            record = {"seq": seq, **_record(ts, request_id, session_id, user_id, event_type, data_json)}
            digest = compute_hash(prev, record)
            conn.execute(
                "INSERT INTO audit_log(seq, ts, request_id, session_id, user_id, event_type, data_json, prev_hash, hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (seq, ts, request_id, session_id, user_id, event_type, data_json, prev, digest),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return seq


def anchor() -> dict | None:
    """Copia la cabeza de la cadena al archivo externo de anclajes."""
    conn = _connect()
    try:
        row = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        return None
    entry = {"seq": row["seq"], "hash": row["hash"], "anchored_at": time.time()}
    with open(config.audit_anchors_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def _load_anchors(path: Path | None = None) -> list[dict]:
    path = path or config.audit_anchors_path()
    if not path.exists():
        return []
    anchors = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                anchors.append(json.loads(line))
            except json.JSONDecodeError:
                anchors.append({"invalid": line})
    return anchors


def verify(db_file: Path | None = None, anchors_file: Path | None = None, key: bytes | None = None) -> dict:
    """Recalcula la cadena completa y la contrasta con los anclajes externos."""
    conn = _connect(db_file)
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
    finally:
        conn.close()
    problems: list[dict] = []
    prev = GENESIS
    expected_seq = 1
    hashes: dict[int, str] = {}
    for r in rows:
        if r["seq"] != expected_seq:
            problems.append({"seq": r["seq"], "tipo": "secuencia_discontinua",
                             "detalle": f"se esperaba seq {expected_seq}: faltan o se insertaron eventos"})
        if r["prev_hash"] != prev:
            problems.append({"seq": r["seq"], "tipo": "enlace_roto", "detalle": "prev_hash no coincide con el evento anterior"})
        record = {"seq": r["seq"], **_record(r["ts"], r["request_id"], r["session_id"], r["user_id"], r["event_type"], r["data_json"])}
        if compute_hash(r["prev_hash"], record, key) != r["hash"]:
            problems.append({"seq": r["seq"], "tipo": "contenido_alterado", "detalle": "el HMAC del evento no coincide"})
        prev = r["hash"]
        hashes[r["seq"]] = r["hash"]
        expected_seq = r["seq"] + 1
    last_seq = rows[-1]["seq"] if rows else 0
    anchors = _load_anchors(anchors_file)
    anchor_problems = 0
    for a in anchors:
        if "invalid" in a:
            problems.append({"seq": None, "tipo": "anclaje_ilegible", "detalle": a["invalid"][:80]})
            anchor_problems += 1
            continue
        if a["seq"] > last_seq:
            problems.append({"seq": a["seq"], "tipo": "truncamiento",
                             "detalle": f"el anclaje registra seq {a['seq']} pero el log termina en {last_seq}"})
            anchor_problems += 1
        elif hashes.get(a["seq"]) != a["hash"]:
            problems.append({"seq": a["seq"], "tipo": "anclaje_no_coincide",
                             "detalle": "el hash anclado difiere: la cadena fue reescrita"})
            anchor_problems += 1
    return {
        "integro": not problems,
        "eventos": len(rows),
        "ultimo_seq": last_seq,
        "cabeza": rows[-1]["hash"] if rows else GENESIS,
        "anclajes_verificados": len(anchors) - anchor_problems,
        "anclajes_total": len(anchors),
        "eventos_sin_anclar": last_seq - max([a.get("seq", 0) for a in anchors if "seq" in a] or [0]),
        "problemas": problems[:200],
    }


def _row_to_dict(r: sqlite3.Row) -> dict:
    return {
        "seq": r["seq"], "ts": r["ts"], "request_id": r["request_id"], "session_id": r["session_id"],
        "user_id": r["user_id"], "event_type": r["event_type"], "data": json.loads(r["data_json"]),
        "prev_hash": r["prev_hash"], "hash": r["hash"],
    }


def query(*, request_id: str | None = None, session_id: str | None = None, user_id: str | None = None,
          event_type: str | None = None, limit: int = 50, scope_user: str | None = None) -> list[dict]:
    clauses, params = [], []
    for column, value in (("request_id", request_id), ("session_id", session_id), ("user_id", user_id), ("event_type", event_type)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if scope_user:
        clauses.append("user_id = ?")
        params.append(scope_user)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = _connect()
    try:
        rows = conn.execute(f"SELECT * FROM audit_log {where} ORDER BY seq DESC LIMIT ?", (*params, limit)).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in reversed(rows)]


def events_for_request(request_id: str) -> list[dict]:
    return query(request_id=request_id, limit=10_000)


def request_completeness(request_id: str) -> dict:
    events = events_for_request(request_id)
    types = {e["event_type"] for e in events}
    missing = sorted(REQUIRED_PER_REQUEST - types)
    tool_calls = [e for e in events if e["event_type"] == "tool_call"]
    # Toda llamada a herramienta debe tener decisión de autorización y duración registradas.
    tool_ok = all("autorizacion" in e["data"] and "duracion_ms" in e["data"] for e in tool_calls)
    final = next((e for e in events if e["event_type"] == "final_result"), None)
    final_ok = bool(final and "estado" in final["data"] and "latencia_ms" in final["data"])
    route_ok = "route_selected" in types or "confirmation_decision" in types
    complete = not missing and tool_ok and final_ok and route_ok
    return {"request_id": request_id, "completo": complete, "faltantes": missing, "eventos": len(events),
            "herramientas_con_autorizacion": tool_ok, "resultado_final_ok": final_ok, "ruta_registrada": route_ok}


def export_session(session_id: str) -> dict:
    events = query(session_id=session_id, limit=100_000)
    request_ids = []
    for e in events:
        if e["request_id"] and e["request_id"] not in request_ids:
            request_ids.append(e["request_id"])
    integrity = verify()
    return {
        "formato": "labbot-audit-export/1",
        "generado": datetime.now().isoformat(timespec="seconds"),
        "sesion_id": session_id,
        "aviso": "Exportación sin datos biométricos: no contiene imágenes, plantillas ni tokens.",
        "solicitudes": [
            {"request_id": rid, "completitud": request_completeness(rid),
             "eventos": [e for e in events if e["request_id"] == rid]}
            for rid in request_ids
        ],
        "eventos_de_sesion": [e for e in events if not e["request_id"]],
        "verificacion_integridad": {k: integrity[k] for k in ("integro", "eventos", "ultimo_seq", "cabeza", "anclajes_verificados", "anclajes_total", "eventos_sin_anclar")},
        "problemas_integridad": integrity["problemas"],
    }
