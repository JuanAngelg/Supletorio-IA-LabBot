"""Auditoría: solo adición, detección de alteraciones, completitud y exportación sin biometría."""
import json
import sqlite3

import pytest

from labbot import audit, config


@pytest.fixture
def activity(make_session, orch, reserve):
    reserve("est-002")
    sid = make_session("est-001")
    orch.handle_message(sid, "¿Qué requisitos debo cumplir para solicitar un osciloscopio?", "router")
    orch.handle_message(sid, "Cancele la reserva RES-0001", "agente")
    r = orch.handle_message(sid, "reserve OSC-02 el 2026-11-03 de 10:00 a 12:00", "router")
    orch.confirm(sid, r["pendiente"]["id"], "confirmar")
    return sid


def _raw():
    conn = sqlite3.connect(config.audit_db_path(), isolation_level=None)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("DROP TRIGGER audit_no_delete")
    return conn


def test_chain_is_valid(activity):
    assert audit.verify()["integro"]


def test_app_cannot_update_or_delete(activity):
    conn = sqlite3.connect(config.audit_db_path(), isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE audit_log SET event_type = 'x' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM audit_log WHERE seq = 1")


def test_detects_modification(activity):
    _raw().execute("UPDATE audit_log SET data_json = '{}' WHERE seq = 5")
    res = audit.verify()
    assert not res["integro"] and any(p["tipo"] == "contenido_alterado" and p["seq"] == 5 for p in res["problemas"])


def test_detects_deletion(activity):
    _raw().execute("DELETE FROM audit_log WHERE seq = 7")
    tipos = {p["tipo"] for p in audit.verify()["problemas"]}
    assert {"secuencia_discontinua", "enlace_roto"} <= tipos


def test_detects_tail_truncation_with_anchor(activity):
    _raw().execute("DELETE FROM audit_log WHERE seq > (SELECT MAX(seq) - 3 FROM audit_log)")
    assert any(p["tipo"] == "truncamiento" for p in audit.verify()["problemas"])


def test_detects_rewrite_with_key_via_anchors(activity):
    conn = _raw()
    rows = conn.execute("SELECT seq, ts, request_id, session_id, user_id, event_type, data_json FROM audit_log ORDER BY seq").fetchall()
    prev = audit.GENESIS
    for seq, ts, rid, sid, uid, et, data in rows:
        data = '{"alterado":true}' if et == "authz_denied" else data
        rec = {"seq": seq, "ts": ts, "request_id": rid, "session_id": sid, "user_id": uid, "event_type": et, "data": data}
        h = audit.compute_hash(prev, rec)
        conn.execute("UPDATE audit_log SET data_json=?, prev_hash=?, hash=? WHERE seq=?", (data, prev, h, seq))
        prev = h
    res = audit.verify()
    assert any(p["tipo"] == "anclaje_no_coincide" for p in res["problemas"])


def test_every_chat_request_is_complete(activity):
    events = audit.query(session_id=activity, limit=10_000)
    rids = {e["request_id"] for e in events if e["request_id"]}
    assert rids
    for rid in rids:
        assert audit.request_completeness(rid)["completo"], rid


def test_export_contains_no_biometric_data(activity):
    data = audit.export_session(activity)
    text = json.dumps(data, ensure_ascii=False)
    for forbidden in ('"frames"', '"embedding"', '"evidence_token"', '"template"', '"totp_secret"', "data:image"):
        assert forbidden not in text
    assert data["verificacion_integridad"]["integro"]
    kinds = {e["event_type"] for r in data["solicitudes"] for e in r["eventos"]}
    assert {"request_received", "route_selected", "tool_call", "rag_evidence", "authz_denied",
            "confirmation_requested", "confirmation_decision", "db_change", "final_result"} <= kinds


def test_sanitize_strips_sensitive_keys():
    clean = audit.sanitize({"frames": ["abc"], "nested": {"evidence_token": "x", "ok": 1}, "code": "NO_AUTORIZADO"})
    assert clean == {"frames": "[omitido]", "nested": {"evidence_token": "[omitido]", "ok": 1}, "code": "NO_AUTORIZADO"}
