"""Utilidades para pruebas automatizadas y evaluación (no se usan en producción)."""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

FIXED_NOW = "2026-11-02T09:00"  # lunes


def configure(var_dir: Path, *, llm: str = "mock", mcp: str = "inprocess", now: str = FIXED_NOW) -> None:
    os.environ["LABBOT_VAR_DIR"] = str(var_dir)
    os.environ["LLM_PROVIDER"] = llm
    os.environ["MCP_TRANSPORT"] = mcp
    os.environ["FACE_ENGINE"] = "simulated"
    os.environ["LABBOT_NOW"] = now
    for name in ("LABBOT_DB", "LABBOT_AUDIT_DB", "LABBOT_AUDIT_ANCHORS", "LABBOT_APP_DB", "LABBOT_CHROMA_DIR", "FACE_DB"):
        os.environ.pop(name, None)
    reset_caches()


def reset_caches() -> None:
    from . import llm, rag
    rag.get_embedder.cache_clear()
    rag._cached_collection.cache_clear()
    llm.get_llm.cache_clear()
    try:
        from face_service import app as face_app
        face_app.engine.cache_clear()
    except ImportError:  # pragma: no cover
        pass


def bootstrap(ingest_docs: bool = True) -> None:
    from . import audit, db, faults, rag, security
    db.init_schema()
    db.reset_business_data()
    db.reset_app_data()
    audit.init()
    faults.clear()
    security.ensure_face_keypair()
    if ingest_docs:
        rag.ingest()


def use_face_test_client() -> None:
    """Conecta el backend al servicio facial en proceso (motor simulado)."""
    from fastapi.testclient import TestClient

    from face_service.app import app as face_app
    from . import auth
    auth.set_face_client(auth.FaceClient(TestClient(face_app)))


def authenticate(session_id: str, user_id: str) -> None:
    """Autentica una sesión con una evidencia firmada por la clave del servicio facial (fixture)."""
    from . import auth, security, sessions
    now = time.time()
    token = security.sign_evidence({
        "iss": "face-service", "aud": "labbot-backend", "sub": user_id, "sid": session_id, "vid": "fixture",
        "amr": ["face", "liveness"], "liveness": "aprobada", "iat": now, "exp": now + 900,
        "jti": secrets.token_hex(16), "motor": "fixture-evaluacion",
    })
    auth.bind_face_evidence(sessions.get_session(session_id), token)


def sim_frame(subject: str, yaw: float = 0.0, noise: int = 0, **extra) -> str:
    """Fotograma simulado codificado en base64 para el motor de pruebas."""
    import base64
    payload = {"sujeto": subject, "yaw": yaw, "ruido": noise, **extra}
    return base64.b64encode(json.dumps(payload).encode()).decode()


YAW_FOR = {"frente": 0.0, "izquierda": 0.35, "derecha": -0.35}
