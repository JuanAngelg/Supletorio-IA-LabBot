"""Servicio facial: enrolamiento con consentimiento, verificación 1:1, presencia y suplantación."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from face_service import store
from face_service.app import app as face_app
from labbot import auth, config, db, security, sessions, testing
from labbot.domain import service

KEY = {"X-Service-Key": None}


@pytest.fixture
def fc(lab):
    KEY["X-Service-Key"] = config.secret("FACE_SERVICE_KEY")
    return TestClient(face_app)


def _ticket(user, purpose="enrolar"):
    return security.sign_enroll_ticket(user_id=user, purpose=purpose)


def _enroll(fc, user="est-001"):
    r = fc.post("/v1/enrollments", json={"ticket": _ticket(user), "consentimiento": {"aceptado": True},
                                         "frames": [testing.sim_frame(user, 0.0, i) for i in range(4)]})
    assert r.status_code == 200, r.text


def _frames(subject, challenge, mode="ok"):
    out = []
    for i, step in enumerate(challenge):
        for k in range(3):
            yaw = testing.YAW_FOR[step]
            extra = {}
            if mode == "foto":
                yaw = 0.02 * k  # una foto inclinada no cambia el cociente de giro
            if mode == "orden":
                yaw = -testing.YAW_FOR[step]  # video grabado con otra secuencia de giros
            if mode == "fija":
                extra = {"huella": "misma-imagen"}
            if mode == "dos_rostros" and i == 1:
                extra = {"rostros": 2}
            out.append({"paso": i, "imagen": testing.sim_frame(subject, yaw, i * 10 + k, **extra)})
    return out


def _start(fc, user="est-001", sid="ses_x"):
    r = fc.post("/v1/verifications", json={"user_id": user, "session_id": sid}, headers=KEY)
    assert r.status_code == 200, r.text
    return r.json()


def test_enrollment_requires_consent_and_ticket(fc):
    frames = [testing.sim_frame("est-001", 0.0, i) for i in range(4)]
    assert fc.post("/v1/enrollments", json={"ticket": _ticket("est-001"), "consentimiento": {"aceptado": False},
                                            "frames": frames}).status_code == 400
    assert fc.post("/v1/enrollments", json={"ticket": "x.y", "consentimiento": {"aceptado": True},
                                            "frames": frames}).status_code == 401
    assert fc.post("/v1/enrollments", json={"ticket": _ticket("est-001", "eliminar"), "consentimiento": {"aceptado": True},
                                            "frames": frames}).status_code == 401


def test_template_is_encrypted_at_rest_and_deletable(fc):
    _enroll(fc)
    raw = sqlite3.connect(store.db_path()).execute("SELECT template_enc FROM templates").fetchone()[0]
    assert raw.startswith(b"gAAAA")  # token Fernet, no floats en claro
    r = fc.request("DELETE", "/v1/enrollments/est-001", json={"ticket": _ticket("est-001", "eliminar")})
    assert r.json() == {"eliminado": True}
    assert store.load_template("est-001") is None
    assert fc.post("/v1/verifications", json={"user_id": "est-001", "session_id": "s"}, headers=KEY).status_code == 409


@pytest.mark.parametrize("mode,reason", [
    ("ok", "verificado"), ("foto", "pose_incorrecta"), ("orden", "pose_incorrecta"),
    ("fija", "fotogramas_repetidos"), ("dos_rostros", "multiples_rostros"),
])
def test_liveness_and_spoofing(fc, mode, reason):
    _enroll(fc)
    v = _start(fc)
    r = fc.post(f"/v1/verifications/{v['verification_id']}/frames", json={"frames": _frames("est-001", v["desafio"], mode)})
    assert r.json()["motivo"] == reason


def test_impostor_is_rejected(fc):
    _enroll(fc, "est-001")
    v = _start(fc, "est-001")
    r = fc.post(f"/v1/verifications/{v['verification_id']}/frames", json={"frames": _frames("est-002", v["desafio"])})
    assert r.json()["estado"] == "rechazada"


def test_verification_is_single_use_and_locks_after_failures(fc):
    _enroll(fc)
    v = _start(fc)
    url = f"/v1/verifications/{v['verification_id']}/frames"
    fc.post(url, json={"frames": _frames("est-002", v["desafio"])})
    assert fc.post(url, json={"frames": _frames("est-001", v["desafio"])}).status_code == 409
    for _ in range(2):
        v = _start(fc)
        fc.post(f"/v1/verifications/{v['verification_id']}/frames", json={"frames": _frames("est-002", v["desafio"])})
    r = fc.post("/v1/verifications", json={"user_id": "est-001", "session_id": "s"}, headers=KEY)
    assert r.status_code == 429


def test_backend_requires_service_key(fc):
    assert fc.post("/v1/verifications", json={"user_id": "est-001", "session_id": "s"}).status_code == 401


def test_full_flow_binds_session_and_fallback(fc, make_session):
    _enroll(fc)
    sid = make_session("est-001", authenticated=False)
    s = sessions.get_session(sid)
    v = auth.start_face(s)
    fc.post(f"/v1/verifications/{v['verification_id']}/frames", json={"frames": _frames("est-001", v["desafio"])})
    out = auth.complete_face(sessions.get_session(sid))
    assert out["autenticado"] and sessions.get_session(sid).auth_method == "facial"
    # Mecanismo alternativo con TOTP en otra sesión
    sid2 = make_session("est-002", authenticated=False)
    with db.business() as conn:
        secret = service.get_user(conn, "est-002")["totp_secret"]
    with pytest.raises(auth.AuthError):
        auth.fallback_totp(sessions.get_session(sid2), "000000" if security.totp(secret) != "000000" else "111111")
    assert auth.fallback_totp(sessions.get_session(sid2), security.totp(secret))["autenticado"]
