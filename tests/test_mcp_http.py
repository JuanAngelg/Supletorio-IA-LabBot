"""Servidor MCP real por Streamable HTTP: catálogo, llamadas y caída del servicio (escenario D)."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from labbot import mcp_client, security
from labbot.errors import ServiceUnavailable

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mcp_server(lab, monkeypatch):
    port = _free_port()
    env = {**os.environ, "MCP_PORT": str(port), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "mcp_server.server"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_URL", f"http://127.0.0.1:{port}/mcp")
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    yield proc
    proc.kill()
    proc.wait()


def _ctx(auth=True):
    return security.sign_context(user_id="est-001", role="estudiante", session_id="ses_t", request_id="req_t",
                                 authenticated=auth)


def test_catalog_has_five_tools(mcp_server):
    names = {t["name"] for t in mcp_client.list_tools()}
    assert {"buscar_reglamento", "consultar_disponibilidad", "crear_reserva", "cancelar_reserva",
            "consultar_auditoria"} <= names


def test_calls_over_http_and_server_crash(mcp_server, make_session, orch):
    r = mcp_client.call_tool("consultar_disponibilidad", {"ctx_token": _ctx(), "fecha": "2026-11-03",
                                                          "hora_inicio": "10:00", "hora_fin": "12:00",
                                                          "tipo_equipo": "osciloscopio"})
    assert r["status"] == "ok" and len(r["data"]["equipos"]) == 3
    r = mcp_client.call_tool("buscar_reglamento", {"ctx_token": _ctx(False), "consulta": "requisitos osciloscopio"})
    assert r["data"]["fragmentos"][0]["chunk_id"] == "REG-LAB-001#art3"

    sid = make_session("est-001")
    pending = orch.handle_message(sid, "reserve OSC-01 el 2026-11-03 de 10:00 a 12:00", "router")["pendiente"]
    mcp_server.kill()
    mcp_server.wait()
    with pytest.raises(ServiceUnavailable):
        mcp_client.call_tool("consultar_disponibilidad", {"ctx_token": _ctx(), "fecha": "2026-11-03",
                                                          "hora_inicio": "10:00", "hora_fin": "12:00"}, timeout=3)
    out = orch.confirm(sid, pending["id"], "confirmar")
    assert out["estado"] == "fallo_servicio"
    assert "NO se registró" in out["respuesta"]
