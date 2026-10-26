"""API del chatbot (puerto 8000) y servidor de la interfaz web.

Toda petición de una sesión lleva X-Session-Id y X-Session-Token (el token se guarda
solo como hash). La identidad se *declara* al crear la sesión; la autenticación facial o
el código alternativo la verifican después.

Ejecución:  uvicorn labbot.api:app --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import audit, auth, config, db, faults, security, sessions
from .domain import policy, service
from .errors import DomainError, ServiceUnavailable
from .orchestrator.orchestrator import orchestrator

STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app):
    db.init_schema()
    audit.init()
    with db.connect_app():
        pass
    try:
        with db.business() as conn:
            empty = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if empty:
            db.reset_business_data()
    except ServiceUnavailable:
        pass
    yield


app = FastAPI(title="LabBot — asistente auditable del laboratorio", version="1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(DomainError)
async def _domain_error(_req, exc: DomainError):
    return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=403 if exc.category == "no_autorizado" else 400)


@app.exception_handler(ServiceUnavailable)
async def _unavailable(_req, exc: ServiceUnavailable):
    return JSONResponse({"detail": f"{exc.service} no disponible", "code": "SERVICIO_NO_DISPONIBLE"}, status_code=503)


def current_session(x_session_id: str = Header(...), x_session_token: str = Header(...)) -> sessions.Session:
    return sessions.get_session(x_session_id, x_session_token, check_token=True)


# --- modelos ---------------------------------------------------------------
class NewSession(BaseModel):
    user_id: str


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    strategy: Literal["router", "agente"] = "router"


class ConfirmIn(BaseModel):
    pending_id: str
    decision: Literal["confirmar", "rechazar"]


class FaceComplete(BaseModel):
    verification_id: Optional[str] = None


class FallbackIn(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class TicketIn(BaseModel):
    purpose: Literal["enrolar", "eliminar"]


class FaultIn(BaseModel):
    name: str
    value: bool | list[str]


# --- interfaz y configuración ---------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def get_config():
    return {"face_url": config.face_public_url(), "demo_mode": config.demo_mode(), "strategies": ["router", "agente"],
            "llm": {"proveedor": config.llm_provider(), "modelo": config.llm_model()}}


@app.get("/api/users")
def users():
    """Cuentas de prueba para declarar la identidad (no autentica a nadie)."""
    return service.list_users()


# --- sesiones --------------------------------------------------------------
@app.post("/api/sessions")
def new_session(body: NewSession):
    session, token = sessions.create_session(body.user_id)
    return {"session_id": session.id, "token": token, "user_id": session.user_id, "user_name": session.user_name,
            "role": session.role, "autenticacion": session.auth_state()}


@app.get("/api/sessions")
def my_sessions(s: sessions.Session = Depends(current_session)):
    return sessions.list_sessions(s.user_id)


@app.get("/api/session")
def session_info(s: sessions.Session = Depends(current_session)):
    return {"session_id": s.id, "user_id": s.user_id, "user_name": s.user_name, "role": s.role,
            "autenticacion": s.auth_state(), "mensajes": sessions.messages(s.id)}


# --- conversación ----------------------------------------------------------
@app.post("/api/chat")
def chat(body: ChatIn, s: sessions.Session = Depends(current_session)):
    return orchestrator.handle_message(s.id, body.message, body.strategy)


@app.post("/api/confirm")
def confirm(body: ConfirmIn, s: sessions.Session = Depends(current_session)):
    return orchestrator.confirm(s.id, body.pending_id, body.decision)


# --- autenticación -----------------------------------------------------------
@app.post("/api/auth/face/start")
def face_start(s: sessions.Session = Depends(current_session)):
    try:
        data = auth.start_face(s)
    except auth.AuthError as exc:
        raise HTTPException(409, {"code": exc.code, "message": exc.message}) from exc
    return {"verification_id": data["verification_id"], "desafio": data["desafio"], "face_url": config.face_public_url(),
            "expira_en_s": data["expira_en_s"]}


@app.post("/api/auth/face/complete")
def face_complete(body: FaceComplete, s: sessions.Session = Depends(current_session)):
    try:
        return auth.complete_face(s, body.verification_id)
    except auth.AuthError as exc:
        raise HTTPException(400, {"code": exc.code, "message": exc.message}) from exc


@app.post("/api/auth/fallback")
def fallback(body: FallbackIn, s: sessions.Session = Depends(current_session)):
    try:
        return auth.fallback_totp(s, body.code)
    except auth.AuthError as exc:
        raise HTTPException(401, {"code": exc.code, "message": exc.message}) from exc


@app.get("/api/auth/state")
def auth_state(s: sessions.Session = Depends(current_session)):
    return s.auth_state()


@app.post("/api/biometrics/ticket")
def biometric_ticket(body: TicketIn, s: sessions.Session = Depends(current_session)):
    try:
        ticket = auth.enrollment_ticket(s, body.purpose)
    except auth.AuthError as exc:
        raise HTTPException(401, {"code": exc.code, "message": exc.message}) from exc
    return {"ticket": ticket, "face_url": config.face_public_url(), "user_id": s.user_id}


# --- auditoría ---------------------------------------------------------------
def _audit_actor(s: sessions.Session) -> policy.Actor:
    actor = policy.Actor(s.user_id, s.role, s.authenticated)
    if not s.authenticated:
        raise HTTPException(401, "Verifique su identidad para consultar la auditoría")
    return actor


@app.get("/api/audit/request/{request_id}")
def audit_request(request_id: str, s: sessions.Session = Depends(current_session)):
    events = audit.events_for_request(request_id)
    if events and s.role != "auditor" and any(e["session_id"] != s.id for e in events):
        if not (s.authenticated and all(e["user_id"] == s.user_id for e in events)):
            raise HTTPException(403, "Solo el auditor puede ver solicitudes de otras sesiones")
    return {"request_id": request_id, "completitud": audit.request_completeness(request_id), "eventos": events}


@app.get("/api/audit/session/{session_id}/export")
def audit_export(session_id: str, s: sessions.Session = Depends(current_session)):
    if session_id != s.id:
        actor = _audit_actor(s)
        if actor.role != "auditor":
            raise HTTPException(403, "Solo el auditor puede exportar otras sesiones")
    data = audit.export_session(session_id)
    audit.log("audit_exported", session_id=s.id, user_id=s.user_id, sesion_exportada=session_id)
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="auditoria_{session_id}.json"'})


@app.get("/api/audit/verify")
def audit_verify(s: sessions.Session = Depends(current_session)):
    actor = _audit_actor(s)
    if actor.role != "auditor":
        raise HTTPException(403, "Solo el auditor puede verificar la integridad global")
    return audit.verify()


# --- demostración (solo DEMO_MODE) ---------------------------------------------
def _demo():
    if not config.demo_mode():
        raise HTTPException(404)


@app.get("/api/demo/state", dependencies=[Depends(_demo)])
def demo_state():
    try:
        snap = service.snapshot()
    except ServiceUnavailable:
        snap = {"error": "base de datos de operaciones no disponible"}
    return {"bd": snap, "fallos": faults.load()}


@app.post("/api/demo/faults", dependencies=[Depends(_demo)])
def demo_faults(body: FaultIn):
    data = faults.set_fault(body.name, body.value)
    audit.log("fault_injected", fallo=body.name, valor=body.value)
    return data


@app.post("/api/demo/reset", dependencies=[Depends(_demo)])
def demo_reset():
    faults.clear()
    db.reset_business_data()
    audit.log("demo_reset", detalle="datos de operaciones restablecidos al estado semilla")
    return {"ok": True}
