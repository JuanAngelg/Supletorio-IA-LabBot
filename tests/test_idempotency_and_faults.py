"""Prevención de duplicados ante reintentos y escenario D (fallo parcial)."""
import threading

from labbot import audit, faults
from labbot.domain import policy, service

MSG = "reserve OSC-02 el 2026-11-03 de 10:00 a 12:00"


def _active():
    return [r for r in service.snapshot()["reservas"] if r["status"] == "activa"]


def test_same_idempotency_key_never_duplicates(lab):
    actor = policy.Actor("est-001", "estudiante", True)
    first = service.create_reservation(actor, "OSC-02", "2026-11-03", "10:00", "12:00", "clave-1")
    again = service.create_reservation(actor, "OSC-02", "2026-11-03", "10:00", "12:00", "clave-1")
    assert first["reserva_id"] == again["reserva_id"]
    assert first["repetida"] is False and again["repetida"] is True
    assert len(_active()) == 1


def test_idempotency_key_reuse_with_other_data_is_rejected(lab):
    actor = policy.Actor("est-001", "estudiante", True)
    service.create_reservation(actor, "OSC-02", "2026-11-03", "10:00", "12:00", "clave-2")
    try:
        service.create_reservation(actor, "OSC-01", "2026-11-03", "10:00", "12:00", "clave-2")
        raise AssertionError("debió rechazarse")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "IDEMPOTENCIA_CONFLICTO"


def test_double_click_confirmation_creates_one_reservation(make_session, orch):
    sid = make_session("est-001")
    pid = orch.handle_message(sid, MSG, "router")["pendiente"]["id"]
    barrier = threading.Barrier(2)
    out = []

    def go():
        barrier.wait()
        out.append(orch.confirm(sid, pid, "confirmar"))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(_active()) == 1
    assert any(r["estado"] == "completada" for r in out)


def test_repeated_llm_call_reuses_pending_operation(make_session, orch):
    sid = make_session("est-001")
    p1 = orch.handle_message(sid, MSG, "agente")["pendiente"]["id"]
    p2 = orch.handle_message(sid, MSG, "router")["pendiente"]["id"]
    assert p1 == p2


def test_lost_mcp_response_is_reconciled_without_duplicate(make_session, orch):
    sid = make_session("est-001")
    pid = orch.handle_message(sid, MSG, "router")["pendiente"]["id"]
    faults.set_fault("mcp_drop_response", ["crear_reserva"])
    r = orch.confirm(sid, pid, "confirmar")
    faults.clear()
    assert r["estado"] == "completada"
    assert "sin duplicar" in r["respuesta"]
    assert len(_active()) == 1
    rid = r["request_id"]
    types = [e["event_type"] for e in audit.events_for_request(rid)]
    assert types.count("retry") == 3 and "reconciliation" in types


def test_mcp_down_reports_real_state_and_allows_safe_retry(make_session, orch):
    sid = make_session("est-001")
    pid = orch.handle_message(sid, MSG, "router")["pendiente"]["id"]
    faults.set_fault("mcp_down", True)
    r = orch.confirm(sid, pid, "confirmar")
    assert r["estado"] == "fallo_servicio"
    assert "NO se registró" in r["respuesta"]
    assert not _active()
    faults.clear()
    r2 = orch.confirm(sid, pid, "confirmar")
    assert r2["estado"] == "completada"
    assert len(_active()) == 1


def test_db_down_during_confirmation_never_claims_success(make_session, orch):
    sid = make_session("est-001")
    pid = orch.handle_message(sid, MSG, "router")["pendiente"]["id"]
    faults.set_fault("db_down", True)
    r = orch.confirm(sid, pid, "confirmar")
    faults.clear()
    assert r["estado"] == "fallo_servicio"
    assert "NO la dé por realizada" in r["respuesta"]
    assert not _active()


def test_db_down_during_query(make_session, orch):
    sid = make_session("est-001")
    faults.set_fault("db_down", True)
    for strategy in ("router", "agente"):
        r = orch.handle_message(sid, "qué osciloscopios hay disponibles el 2026-11-03 de 10:00 a 12:00", strategy)
        assert r["estado"] == "fallo_servicio", r
        assert r["pendiente"] is None


def test_llm_down_is_reported(make_session, orch):
    sid = make_session("est-001")
    faults.set_fault("llm_down", True)
    r = orch.handle_message(sid, MSG, "agente")
    assert r["estado"] == "fallo_servicio" and "llm" in r["respuesta"]
