"""Autenticación en el backend: cliente del servicio facial y mecanismo alternativo.

Garantías:
- La sesión solo queda autenticada cuando el backend verifica criptográficamente una
  evidencia (firma Ed25519 del servicio facial, o TOTP válido en el formulario seguro).
- La evidencia debe ser de esta sesión y de la identidad declarada, no estar vencida y no
  haberse usado antes (jti de un solo uso).
- Ningún texto del LLM ni del usuario cambia el estado de autenticación.
"""
from __future__ import annotations

import secrets
import time

import httpx

from . import audit, config, db, faults, security, sessions
from .domain import service
from .errors import ServiceUnavailable

MAX_FALLBACK_FAILURES = 5


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FaceClient:
    def __init__(self, http: httpx.Client | None = None):
        self._http = http

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(base_url=config.face_service_url(), timeout=10)
        return self._http

    def _headers(self) -> dict:
        return {"X-Service-Key": config.secret("FACE_SERVICE_KEY")}

    def _call(self, method: str, url: str, **kw) -> httpx.Response:
        if faults.active("face_down"):
            raise ServiceUnavailable("servicio_facial", "fallo inyectado")
        try:
            resp = self.http.request(method, url, headers=self._headers(), **kw)
        except httpx.HTTPError as exc:
            raise ServiceUnavailable("servicio_facial", exc.__class__.__name__) from exc
        if resp.status_code >= 500:
            raise ServiceUnavailable("servicio_facial", f"HTTP {resp.status_code}")
        return resp

    def start(self, user_id: str, session_id: str) -> dict:
        resp = self._call("POST", "/v1/verifications", json={"user_id": user_id, "session_id": session_id})
        if resp.status_code == 409:
            raise AuthError("NO_ENROLADO", "La persona no tiene un rostro enrolado; use el código alternativo.")
        if resp.status_code == 429:
            raise AuthError("BLOQUEADO", "Demasiados intentos fallidos; use el código alternativo.")
        resp.raise_for_status()
        return resp.json()

    def evidence(self, vid: str) -> dict:
        resp = self._call("GET", f"/v1/verifications/{vid}/evidence")
        if resp.status_code == 404:
            raise AuthError("VERIFICACION_INEXISTENTE", "No existe esa verificación.")
        resp.raise_for_status()
        return resp.json()


_client = FaceClient()


def set_face_client(client: FaceClient) -> None:
    global _client
    _client = client


def face_client() -> FaceClient:
    return _client


def start_face(session: sessions.Session, request_id: str | None = None) -> dict:
    try:
        data = _client.start(session.user_id, session.id)
    except AuthError as exc:
        audit.log("auth_face_started", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  resultado="no_iniciada", motivo=exc.code)
        raise
    sessions.set_face_verification(session.id, data["verification_id"])
    audit.log("auth_face_started", request_id=request_id, session_id=session.id, user_id=session.user_id,
              resultado="desafio_emitido", verification_id=data["verification_id"], pasos=len(data["desafio"]))
    return data


def bind_face_evidence(session: sessions.Session, token: str, request_id: str | None = None) -> dict:
    try:
        payload = security.verify_evidence(token, expected_session=session.id, expected_user=session.user_id)
        if sessions.evidence_used(payload["jti"]):
            raise security.TokenError("Evidencia ya utilizada (reproducción)")
    except security.TokenError as exc:
        audit.log("auth_result", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  metodo="facial", resultado="rechazada", motivo=str(exc))
        raise AuthError("EVIDENCIA_INVALIDA", str(exc)) from exc
    expires = min(payload["exp"], time.time() + config.auth_ttl_seconds())
    sessions.bind_auth(session.id, "facial", payload["jti"], expires)
    audit.log("auth_result", request_id=request_id, session_id=session.id, user_id=session.user_id,
              metodo="facial", resultado="verificada", verification_id=payload.get("vid"),
              evidencia_jti=payload["jti"], vence=expires, comprobacion_presencia=payload.get("liveness"))
    return {"autenticado": True, "metodo": "facial", "vence_en_s": int(expires - time.time())}


def complete_face(session: sessions.Session, vid: str | None = None, request_id: str | None = None) -> dict:
    vid = vid or session.face_verification_id
    if not vid:
        raise AuthError("SIN_VERIFICACION", "No hay una verificación facial iniciada en esta sesión.")
    data = _client.evidence(vid)
    if data.get("session_id") != session.id or data.get("usuario_id") != session.user_id:
        audit.log("auth_result", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  metodo="facial", resultado="rechazada", motivo="verificacion_de_otra_sesion")
        raise AuthError("EVIDENCIA_INVALIDA", "La verificación no corresponde a esta sesión.")
    if data["estado"] == "pendiente":
        return {"autenticado": False, "estado": "pendiente", "motivo": "Aún no se han enviado las capturas."}
    if data["estado"] != "verificada" or not data.get("evidence_token"):
        audit.log("auth_result", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  metodo="facial", resultado="rechazada", motivo=data.get("motivo"), verification_id=vid)
        sessions.set_face_verification(session.id, None)
        return {"autenticado": False, "estado": data["estado"], "motivo": data.get("motivo"),
                "alternativa": "Puede usar el código temporal alternativo."}
    sessions.set_face_verification(session.id, None)
    return bind_face_evidence(session, data["evidence_token"], request_id)


def fallback_totp(session: sessions.Session, code: str, request_id: str | None = None) -> dict:
    """Mecanismo alternativo: código TOTP introducido en un formulario (nunca en el chat)."""
    if sessions.fallback_failures(session.id) >= MAX_FALLBACK_FAILURES:
        audit.log("auth_result", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  metodo="codigo_temporal", resultado="bloqueado")
        raise AuthError("BLOQUEADO", "Demasiados intentos con código; contacte al encargado.")
    with db.business() as conn:
        user = service.get_user(conn, session.user_id)
    if not security.verify_totp(user["totp_secret"], code or ""):
        failures = sessions.register_fallback_failure(session.id)
        audit.log("auth_result", request_id=request_id, session_id=session.id, user_id=session.user_id,
                  metodo="codigo_temporal", resultado="rechazada", fallos=failures)
        raise AuthError("CODIGO_INVALIDO", "Código incorrecto o vencido.")
    jti = "totp_" + secrets.token_hex(12)
    expires = time.time() + config.auth_ttl_seconds()
    sessions.bind_auth(session.id, "codigo_temporal", jti, expires)
    audit.log("auth_result", request_id=request_id, session_id=session.id, user_id=session.user_id,
              metodo="codigo_temporal", resultado="verificada", evidencia_jti=jti, vence=expires)
    return {"autenticado": True, "metodo": "codigo_temporal", "vence_en_s": int(expires - time.time())}


def enrollment_ticket(session: sessions.Session, purpose: str) -> str:
    """Solo una sesión ya autenticada (p. ej. con código alternativo) puede enrolar o borrar su rostro."""
    if not session.authenticated:
        raise AuthError("REQUIERE_AUTENTICACION", "Verifique su identidad con el código alternativo antes de enrolar.")
    audit.log("biometric_" + purpose, session_id=session.id, user_id=session.user_id, resultado="ticket_emitido")
    return security.sign_enroll_ticket(user_id=session.user_id, purpose=purpose)
