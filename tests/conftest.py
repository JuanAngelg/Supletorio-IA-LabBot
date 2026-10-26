import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from labbot import testing  # noqa: E402


@pytest.fixture(scope="session")
def chroma_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("chroma")
    testing.configure(tmp_path_factory.mktemp("bootstrap"))
    os.environ["LABBOT_CHROMA_DIR"] = str(path)
    from labbot import rag
    rag.ingest()
    return path


@pytest.fixture
def lab(tmp_path, chroma_dir):
    """Entorno aislado: BD, auditoría y claves en tmp; LLM simulado; MCP en proceso; motor facial simulado."""
    testing.configure(tmp_path)
    os.environ["LABBOT_CHROMA_DIR"] = str(chroma_dir)
    testing.bootstrap(ingest_docs=False)
    testing.use_face_test_client()
    yield tmp_path
    from labbot import faults
    faults.clear()


@pytest.fixture
def orch(lab):
    from labbot.orchestrator.orchestrator import orchestrator
    return orchestrator


@pytest.fixture
def make_session(lab):
    from labbot import sessions

    def _make(user_id: str, authenticated: bool = True):
        s, _ = sessions.create_session(user_id)
        if authenticated:
            testing.authenticate(s.id, user_id)
        return s.id
    return _make


@pytest.fixture
def reserve(make_session, orch):
    """Crea y confirma una reserva por el flujo completo; devuelve su código."""
    def _reserve(user_id: str, equipo: str = "OSC-01", fecha: str = "2026-11-03", hi: str = "14:00", hf: str = "16:00"):
        sid = make_session(user_id)
        r = orch.handle_message(sid, f"reserve {equipo} el {fecha} de {hi} a {hf}", "router")
        assert r["estado"] == "requiere_confirmacion", r["respuesta"]
        c = orch.confirm(sid, r["pendiente"]["id"], "confirmar")
        assert c["estado"] == "completada", c["respuesta"]
        from labbot.domain import service
        return [x["codigo"] for x in service.snapshot()["reservas"]][-1]
    return _reserve
