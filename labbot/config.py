"""Configuración centralizada.

Los valores se leen de variables de entorno (y de un archivo .env opcional).
Los secretos que no estén definidos se generan una sola vez en var/secrets.json,
de modo que el repositorio nunca contiene credenciales.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def var_dir() -> Path:
    path = Path(env("LABBOT_VAR_DIR", str(ROOT / "var")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def keys_dir() -> Path:
    path = Path(env("LABBOT_KEYS_DIR", str(var_dir() / "keys")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return Path(env("LABBOT_DB", str(var_dir() / "labbot.db")))


def audit_db_path() -> Path:
    return Path(env("LABBOT_AUDIT_DB", str(var_dir() / "audit.db")))


def audit_anchors_path() -> Path:
    # Simula un almacenamiento externo de solo escritura (WORM) para los anclajes.
    return Path(env("LABBOT_AUDIT_ANCHORS", str(var_dir() / "audit_anchors.jsonl")))


def faults_path() -> Path:
    return var_dir() / "faults.json"


def chroma_dir() -> Path:
    return Path(env("LABBOT_CHROMA_DIR", str(var_dir() / "chroma")))


def docs_dir() -> Path:
    return Path(env("LABBOT_DOCS_DIR", str(ROOT / "data" / "docs")))


def seed_path() -> Path:
    return ROOT / "data" / "seed.json"


def secret(name: str) -> str:
    """Devuelve un secreto desde el entorno o lo genera localmente (solo desarrollo)."""
    value = env(name)
    if value:
        return value
    store = keys_dir() / "secrets.json"
    data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    if name not in data:
        data[name] = secrets.token_hex(32)
        store.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data[name]


# --- LLM --------------------------------------------------------------------
def llm_provider() -> str:
    return env("LLM_PROVIDER", "ollama")  # ollama | openai | mock


def llm_base_url() -> str:
    return env("LLM_BASE_URL", "http://localhost:11434/v1")


def llm_model() -> str:
    return env("LLM_MODEL", "gemma4:31b-cloud")


def llm_api_key() -> str:
    return env("LLM_API_KEY", "ollama")


def llm_timeout() -> float:
    return float(env("LLM_TIMEOUT", "120"))


def price_per_mtok() -> tuple[float, float]:
    """Precio de referencia (USD por millón de tokens de entrada/salida) para estimar costo."""
    return float(env("PRICE_IN_PER_MTOK", "0")), float(env("PRICE_OUT_PER_MTOK", "0"))


# --- RAG --------------------------------------------------------------------
def embed_provider() -> str:
    return env("EMBED_PROVIDER", "hashing")  # hashing | ollama


def embed_model() -> str:
    return env("EMBED_MODEL", "nomic-embed-text")


def ollama_url() -> str:
    return env("OLLAMA_URL", "http://localhost:11434")


# --- Servicios --------------------------------------------------------------
def mcp_url() -> str:
    return env("MCP_URL", "http://127.0.0.1:8100/mcp")


def mcp_transport() -> str:
    return env("MCP_TRANSPORT", "http")  # http | inprocess (solo pruebas unitarias)


def face_service_url() -> str:
    return env("FACE_SERVICE_URL", "http://127.0.0.1:8200")


def face_public_url() -> str:
    """URL del servicio facial vista desde el navegador."""
    return env("FACE_PUBLIC_URL", face_service_url())


def demo_mode() -> bool:
    return env_bool("DEMO_MODE", True)


def auth_ttl_seconds() -> int:
    return int(env("AUTH_TTL_SECONDS", "900"))


def pending_ttl_seconds() -> int:
    return int(env("PENDING_TTL_SECONDS", "600"))


# --- Reloj del dominio --------------------------------------------------------
def now() -> datetime:
    """Hora local del laboratorio. LABBOT_NOW permite fijarla para pruebas reproducibles."""
    fixed = env("LABBOT_NOW")
    if fixed:
        return datetime.fromisoformat(fixed)
    return datetime.now().replace(microsecond=0)
