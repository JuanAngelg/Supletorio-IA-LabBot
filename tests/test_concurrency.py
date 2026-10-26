"""Escenario E: reservas concurrentes del mismo equipo y horario. Solo una se confirma."""
import threading

from labbot.domain import policy, service
from labbot.errors import Conflict


def test_two_users_confirm_same_slot_concurrently(make_session, orch):
    a = make_session("est-001")
    b = make_session("est-002")
    msg = "reserve OSC-01 el 2026-11-03 de 14:00 a 16:00"
    pa = orch.handle_message(a, msg, "router")["pendiente"]["id"]
    pb = orch.handle_message(b, msg, "agente")["pendiente"]["id"]
    barrier = threading.Barrier(2)
    results = {}

    def go(sid, pid):
        barrier.wait()
        results[sid] = orch.confirm(sid, pid, "confirmar")

    threads = [threading.Thread(target=go, args=(a, pa)), threading.Thread(target=go, args=(b, pb))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    states = sorted(r["estado"] for r in results.values())
    assert states == ["completada", "rechazada_por_regla"], states
    loser = next(r for r in results.values() if r["estado"] != "completada")
    assert "ya está reservado" in loser["respuesta"]
    active = [r for r in service.snapshot()["reservas"] if r["status"] == "activa"]
    assert len(active) == 1


def test_many_parallel_writers_only_one_wins(lab):
    users = ["est-001", "est-002", "enc-001"]
    outcomes = []
    barrier = threading.Barrier(len(users) * 3)

    def go(uid, i):
        barrier.wait()
        role = "encargado" if uid.startswith("enc") else "estudiante"
        try:
            service.create_reservation(policy.Actor(uid, role, True), "GEN-01", "2026-11-04", "10:00", "12:00", f"k-{uid}-{i}")
            outcomes.append("ok")
        except Conflict:
            outcomes.append("conflicto")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=go, args=(u, i)) for u in users for i in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("ok") == 1, outcomes
    assert len([r for r in service.snapshot()["reservas"] if r["equipment_id"] == "GEN-01"]) == 1


def test_loan_blocked_when_equipment_already_loaned(lab, monkeypatch):
    monkeypatch.setenv("LABBOT_NOW", "2026-11-02T10:00")
    enc = policy.Actor("enc-001", "encargado", True)
    service.register_loan(enc, "est-001", "MUL-01", "loan-1")
    try:
        service.register_loan(enc, "est-002", "MUL-01", "loan-2")
        raise AssertionError("debió rechazarse")
    except Conflict as exc:
        assert exc.code == "EQUIPO_PRESTADO"
