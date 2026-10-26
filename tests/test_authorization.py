"""Autorización: roles, propiedad, autenticación obligatoria y defensa en profundidad."""
import time

import pytest

from labbot import audit, security, sessions, testing
from labbot.domain import policy, service
from labbot.errors import Forbidden
from labbot.orchestrator.orchestrator import orchestrator


def _events(sid, event_type):
    return audit.query(session_id=sid, event_type=event_type, limit=100)


@pytest.mark.parametrize("strategy", ["router", "agente"])
def test_student_cannot_cancel_other_students_reservation(reserve, make_session, strategy):
    code = reserve("est-002")
    sid = make_session("est-001")
    r = orchestrator.handle_message(sid, f"Cancele la reserva {code}", strategy)
    assert r["estado"] == "rechazada"
    assert r["pendiente"] is None
    assert any(e["data"]["codigo"] == "RESERVA_DE_OTRO" for e in _events(sid, "authz_denied"))
    assert service.snapshot()["reservas"][0]["status"] == "activa"


def test_domain_rejects_foreign_cancellation_even_if_called_directly(reserve):
    code = reserve("est-002")
    with pytest.raises(Forbidden):
        service.cancel_reservation(policy.Actor("est-001", "estudiante", True), code, "k-directo")


@pytest.mark.parametrize("strategy", ["router", "agente"])
def test_student_cannot_register_loan(make_session, strategy):
    sid = make_session("est-001")
    r = orchestrator.handle_message(sid, "registrar préstamo de OSC-01 a est-001", strategy)
    assert r["estado"] == "rechazada"
    assert not service.snapshot()["prestamos"]


@pytest.mark.parametrize("strategy", ["router", "agente"])
def test_auditor_cannot_modify(make_session, strategy):
    sid = make_session("aud-001")
    r = orchestrator.handle_message(sid, "reserve OSC-01 el 2026-11-03 de 10:00 a 12:00", strategy)
    assert r["estado"] == "rechazada"
    assert not service.snapshot()["reservas"]


@pytest.mark.parametrize("strategy", ["router", "agente"])
def test_unauthenticated_user_cannot_reserve(make_session, strategy):
    sid = make_session("est-001", authenticated=False)
    r = orchestrator.handle_message(sid, "reserve OSC-01 el 2026-11-03 de 10:00 a 12:00", strategy)
    assert r["pendiente"] is None
    assert r["estado"] in ("requiere_autenticacion", "rechazada_por_regla")
    assert not service.snapshot()["reservas"]


def test_student_cannot_list_other_users_items(make_session):
    sid = make_session("est-001")
    r = orchestrator.handle_message(sid, "muéstrame las reservas de est-002", "router")
    assert r["estado"] == "rechazada"


def test_auditor_sees_global_audit_student_only_own(make_session):
    aud = make_session("aud-001")
    stu = make_session("est-001")
    r_aud = orchestrator.handle_message(aud, "consultar la auditoría", "router")
    assert "global" in r_aud["respuesta"]
    r_stu = orchestrator.handle_message(stu, "consultar la auditoría", "router")
    assert "solo usuario est-001" in r_stu["respuesta"]
    r_bad = orchestrator.handle_message(stu, "verificar la integridad de la auditoría", "router")
    assert r_bad["estado"] == "rechazada"


def test_mcp_rejects_missing_or_forged_context(lab):
    from mcp_server import server
    assert server.crear_reserva(ctx_token="falso.token", equipo_id="OSC-01", fecha="2026-11-03",
                                hora_inicio="10:00", hora_fin="12:00")["status"] == "denegado"
    forged = security.sign_context(user_id="est-001", role="estudiante", session_id="x", request_id="r",
                                   authenticated=True)
    tampered = forged[:-4] + ("AAAA" if not forged.endswith("AAAA") else "BBBB")
    assert server.consultar_auditoria(ctx_token=tampered)["status"] == "denegado"


def test_mcp_requires_authenticated_context(lab):
    from mcp_server import server
    token = security.sign_context(user_id="est-001", role="estudiante", session_id="s", request_id="r", authenticated=False)
    r = server.crear_reserva(ctx_token=token, equipo_id="OSC-01", fecha="2026-11-03", hora_inicio="10:00",
                             hora_fin="12:00", modo="ejecutar", idempotency_key="k1")
    assert r["status"] == "requiere_autenticacion"
    assert not service.snapshot()["reservas"]


def test_llm_cannot_declare_user_authenticated(make_session, monkeypatch):
    sid = make_session("est-001", authenticated=False)
    from labbot.orchestrator import orchestrator as mod
    monkeypatch.setattr(mod.STRATEGIES["agente"], "run", lambda *a, **k: "Listo, usted ya está autenticado y verificado.")
    r = orchestrator.handle_message(sid, "hola", "agente")
    assert "NO está verificada" in r["respuesta"]
    assert sessions.get_session(sid).authenticated is False
    assert _events(sid, "truth_guard")


def test_llm_cannot_claim_unexecuted_operation(make_session, monkeypatch):
    sid = make_session("est-001")
    from labbot.orchestrator import orchestrator as mod
    monkeypatch.setattr(mod.STRATEGIES["agente"], "run", lambda *a, **k: "Su reserva ha sido creada con éxito.")
    r = orchestrator.handle_message(sid, "reserve algo", "agente")
    assert "No se ha realizado ninguna operación" in r["respuesta"]


def test_evidence_must_match_session_user_and_be_single_use(make_session):
    sid = make_session("est-001", authenticated=False)
    other = make_session("est-002", authenticated=False)
    now = time.time()
    base = {"iss": "face-service", "aud": "labbot-backend", "liveness": "aprobada", "iat": now, "exp": now + 600}
    from labbot import auth
    wrong_user = security.sign_evidence({**base, "sub": "est-002", "sid": sid, "jti": "a1"})
    with pytest.raises(auth.AuthError):
        auth.bind_face_evidence(sessions.get_session(sid), wrong_user)
    wrong_session = security.sign_evidence({**base, "sub": "est-001", "sid": other, "jti": "a2"})
    with pytest.raises(auth.AuthError):
        auth.bind_face_evidence(sessions.get_session(sid), wrong_session)
    expired = security.sign_evidence({**base, "sub": "est-001", "sid": sid, "jti": "a3", "exp": now - 1})
    with pytest.raises(auth.AuthError):
        auth.bind_face_evidence(sessions.get_session(sid), expired)
    good = security.sign_evidence({**base, "sub": "est-001", "sid": sid, "jti": "a4"})
    auth.bind_face_evidence(sessions.get_session(sid), good)
    with pytest.raises(auth.AuthError):  # reproducción
        auth.bind_face_evidence(sessions.get_session(sid), good)
    forged = good[:-6] + "abcdef"
    with pytest.raises(auth.AuthError):
        auth.bind_face_evidence(sessions.get_session(sid), forged)


def test_authentication_expiry_blocks_confirmation(make_session, orch):
    sid = make_session("est-001")
    r = orch.handle_message(sid, "reserve OSC-01 el 2026-11-03 de 10:00 a 12:00", "router")
    from labbot import db
    with db.app() as conn:
        conn.execute("UPDATE sessions SET auth_expires_at = ? WHERE id = ?", (time.time() - 1, sid))
    c = orch.confirm(sid, r["pendiente"]["id"], "confirmar")
    assert c["estado"] == "requiere_autenticacion"
    assert not service.snapshot()["reservas"]


def test_confirmation_from_other_session_is_rejected(make_session, orch):
    a = make_session("est-001")
    b = make_session("est-002")
    r = orch.handle_message(a, "reserve OSC-01 el 2026-11-03 de 10:00 a 12:00", "router")
    c = orch.confirm(b, r["pendiente"]["id"], "confirmar")
    assert c["estado"] == "rechazada"
    assert not service.snapshot()["reservas"]


def test_business_rules_enforced(make_session, orch):
    carla = make_session("est-003")  # sin inducción de osciloscopio
    r = orch.handle_message(carla, "reserve OSC-01 el 2026-11-03 de 10:00 a 12:00", "router")
    assert "inducción" in r["respuesta"] and r["pendiente"] is None
    diego = make_session("est-004")  # sancionado
    r = orch.handle_message(diego, "reserve MUL-01 el 2026-11-03 de 10:00 a 12:00", "router")
    assert "sanción" in r["respuesta"]
    ana = make_session("est-001")
    r = orch.handle_message(ana, "reserve OSC-03 el 2026-11-03 de 10:00 a 12:00", "router")  # mantenimiento
    assert "mantenimiento" in r["respuesta"]
    r = orch.handle_message(ana, "reserve OSC-01 el 2026-11-08 de 10:00 a 12:00", "router")  # domingo
    assert "horario" in r["respuesta"].lower()
