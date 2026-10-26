"""Servicio independiente de autenticación facial (puerto 8200).

El chatbot lo usa solo a través de herramientas del orquestador. Flujo:
  backend  -> POST /v1/verifications              (crea desafío para usuario+sesión)
  navegador-> POST /v1/verifications/{id}/frames  (fotogramas directos; nunca pasan por el LLM)
  backend  -> GET  /v1/verifications/{id}/evidence (evidencia firmada Ed25519, una sola vez)

Las llamadas del backend usan X-Service-Key. El navegador solo conoce el id de la
verificación (aleatorio, de un solo uso, vence en 2 minutos).
"""
from __future__ import annotations

import base64
import binascii
import logging
import secrets
import json
import time
from contextlib import asynccontextmanager
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from labbot import config, faults, security

from . import liveness, store
from .engine import FaceEngineError, cosine, create_engine

log = logging.getLogger("face_service")
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s face_service %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

CHALLENGE_TTL = 120
EVIDENCE_TTL = config.auth_ttl_seconds()
MAX_FAILURES = 3
MAX_FRAMES = 40
MAX_FRAME_BYTES = 800_000
CONSENT_VERSION = "POL-BIO-001 v1.0"

@asynccontextmanager
async def lifespan(_app):
    security.ensure_face_keypair()
    store.purge_expired()
    yield


app = FastAPI(title="Servicio de autenticación facial", version="1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in (config.env("FACE_CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000") or "").split(",")],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def fault_injection(request: Request, call_next):
    if faults.active("face_down") and request.url.path != "/health":
        return JSONResponse({"detail": "servicio facial no disponible (fallo inyectado)"}, status_code=503)
    return await call_next(request)


@lru_cache(maxsize=1)
def engine():
    return create_engine()


def _require_service(key: str | None) -> None:
    if not key or not secrets.compare_digest(key, config.secret("FACE_SERVICE_KEY")):
        raise HTTPException(401, "Clave de servicio inválida")


def _decode(b64: str) -> bytes:
    if "," in b64[:64]:
        b64 = b64.split(",", 1)[1]  # data:image/jpeg;base64,...
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "Imagen mal codificada") from exc
    if len(data) > MAX_FRAME_BYTES:
        raise HTTPException(413, "Imagen demasiado grande")
    return data


def _analyze(data: bytes):
    try:
        return engine().analyze(data)
    except FaceEngineError as exc:
        raise HTTPException(422, str(exc)) from exc


# --- modelos ---------------------------------------------------------------
class Consent(BaseModel):
    aceptado: bool
    version: str = CONSENT_VERSION


class EnrollRequest(BaseModel):
    ticket: str
    consentimiento: Consent
    frames: list[str] = Field(min_length=3, max_length=10)


class DeleteRequest(BaseModel):
    ticket: str


class StartVerification(BaseModel):
    user_id: str
    session_id: str


class Frame(BaseModel):
    paso: int = Field(ge=0, le=10)
    imagen: str


class FramesRequest(BaseModel):
    frames: list[Frame] = Field(min_length=1, max_length=MAX_FRAMES)


# --- endpoints -------------------------------------------------------------
@app.get("/health")
def health():
    try:
        name = engine().name
        ready = True
    except FaceEngineError as exc:
        name, ready = str(exc), False
    return {"ok": True, "motor": name, "listo": ready}


@app.post("/v1/enrollments")
def enroll(req: EnrollRequest):
    try:
        ticket = security.verify_enroll_ticket(req.ticket, "enrolar")
    except security.TokenError as exc:
        raise HTTPException(401, f"Ticket inválido: {exc}") from exc
    if not req.consentimiento.aceptado:
        raise HTTPException(400, "Se requiere consentimiento expreso para el enrolamiento")
    observations = [_analyze(_decode(f)) for f in req.frames]  # en memoria; no se guardan
    good = [o for o in observations if o.n_faces == 1 and abs(o.yaw) <= liveness.FRONT_MAX * 1.5
            and o.sharpness >= liveness.MIN_SHARPNESS]
    if len(good) < 3:
        raise HTTPException(422, "Se necesitan al menos 3 fotogramas frontales, nítidos y con un solo rostro")
    embs = [o.embedding for o in good]
    if min(cosine(a, b) for i, a in enumerate(embs) for b in embs[i + 1:]) < 0.5:
        raise HTTPException(422, "Los fotogramas no parecen de la misma persona")
    template = np.mean(embs, axis=0)
    template /= np.linalg.norm(template)
    store.save_template(ticket["sub"], template, engine().name, req.consentimiento.version)
    log.info("enrolamiento usuario=%s fotogramas=%d", ticket["sub"], len(good))
    return {"enrolado": True, "usuario_id": ticket["sub"], "consentimiento": req.consentimiento.version,
            "retencion_dias": store.RETENTION_SECONDS // 86400}


@app.delete("/v1/enrollments/{user_id}")
def delete_enrollment(user_id: str, req: DeleteRequest):
    try:
        ticket = security.verify_enroll_ticket(req.ticket, "eliminar")
    except security.TokenError as exc:
        raise HTTPException(401, f"Ticket inválido: {exc}") from exc
    if ticket["sub"] != user_id:
        raise HTTPException(403, "El ticket no corresponde a ese usuario")
    deleted = store.delete_template(user_id)
    log.info("eliminacion usuario=%s eliminado=%s", user_id, deleted)
    return {"eliminado": deleted}


@app.get("/v1/enrollments/{user_id}")
def enrollment_status(user_id: str, x_service_key: str | None = Header(None)):
    _require_service(x_service_key)
    return store.enrollment_status(user_id)


@app.post("/v1/verifications")
def start_verification(req: StartVerification, x_service_key: str | None = Header(None)):
    _require_service(x_service_key)
    if store.load_template(req.user_id) is None:
        raise HTTPException(409, "no_enrolado")
    if store.recent_failures(req.user_id) >= MAX_FAILURES:
        raise HTTPException(429, "bloqueado_temporalmente")
    vid = "fv_" + secrets.token_urlsafe(18)
    challenge = liveness.new_challenge()
    store.create_verification(vid, req.user_id, req.session_id, challenge, CHALLENGE_TTL)
    log.info("verificacion_creada id=%s usuario=%s", vid, req.user_id)
    return {"verification_id": vid, "desafio": challenge, "expira_en_s": CHALLENGE_TTL,
            "intentos_restantes": MAX_FAILURES - store.recent_failures(req.user_id)}


@app.post("/v1/verifications/{vid}/frames")
def submit_frames(vid: str, req: FramesRequest):
    row = store.get_verification(vid)
    if not row:
        raise HTTPException(404, "Verificación inexistente")
    if row["status"] != "pendiente":
        raise HTTPException(409, "La verificación ya fue utilizada")
    if row["expires_at"] < time.time():
        store.finish_verification(vid, "rechazada", "tiempo_excedido")
        return {"estado": "rechazada", "motivo": "tiempo_excedido"}
    challenge = json.loads(row["challenge_json"])
    template = store.load_template(row["user_id"])
    if template is None:
        store.finish_verification(vid, "rechazada", "no_enrolado")
        return {"estado": "rechazada", "motivo": "no_enrolado"}
    observations: dict[int, list] = {}
    for f in req.frames:
        if f.paso < len(challenge):
            observations.setdefault(f.paso, []).append(_analyze(_decode(f.imagen)))
    result = liveness.evaluate(challenge, observations, template, engine().match_threshold)
    token = jti = None
    if result.verified:
        jti = secrets.token_hex(16)
        now = time.time()
        token = security.sign_evidence({
            "iss": "face-service", "aud": "labbot-backend", "sub": row["user_id"], "sid": row["session_id"],
            "vid": vid, "amr": ["face", "liveness"], "liveness": "aprobada", "iat": now, "exp": now + EVIDENCE_TTL,
            "jti": jti, "motor": engine().name,
        })
    status = "verificada" if result.verified else "rechazada"
    if not store.finish_verification(vid, status, result.reason, token, jti):
        raise HTTPException(409, "La verificación ya fue utilizada")
    log.info("verificacion id=%s usuario=%s estado=%s motivo=%s", vid, row["user_id"], status, result.reason)
    remaining = max(0, MAX_FAILURES - store.recent_failures(row["user_id"]))
    return {"estado": status, "motivo": result.reason, "comprobaciones": result.checks, "intentos_restantes": remaining}


@app.get("/v1/verifications/{vid}/evidence")
def get_evidence(vid: str, x_service_key: str | None = Header(None)):
    _require_service(x_service_key)
    row = store.get_verification(vid)
    if not row:
        raise HTTPException(404, "Verificación inexistente")
    token = store.take_evidence(vid) if row["status"] == "verificada" else None
    return {"estado": row["status"], "motivo": row["reason"], "usuario_id": row["user_id"],
            "session_id": row["session_id"], "evidence_token": token}
