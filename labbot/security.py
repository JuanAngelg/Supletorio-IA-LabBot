"""Primitivas de seguridad compartidas.

- Evidencia de autenticación facial: token firmado con Ed25519 por el servicio facial.
  El backend solo tiene la clave pública: puede verificarla pero no fabricarla, y el LLM
  nunca la ve. Contiene sujeto, sesión, vencimiento y un jti de un solo uso.
- Token de contexto (backend -> servidor MCP): HMAC con secreto compartido. Lo agrega el
  orquestador al invocar una herramienta MCP; el LLM no puede generarlo ni modificarlo.
- Ticket de enrolamiento (backend -> servicio facial): HMAC, corta duración.
- TOTP (RFC 6238) para el mecanismo alternativo cuando falla la verificación facial.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

from . import config


class TokenError(Exception):
    pass


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


# --- Ed25519 (evidencia facial) ------------------------------------------------
def face_private_key_path() -> Path:
    return Path(config.env("FACE_PRIVATE_KEY", str(config.keys_dir() / "face_ed25519.pem")))


def face_public_key_path() -> Path:
    return Path(config.env("FACE_PUBLIC_KEY", str(config.keys_dir() / "face_ed25519.pub.pem")))


def ensure_face_keypair() -> None:
    """Lo llama el servicio facial al arrancar. La clave privada no sale de ese servicio."""
    priv_path, pub_path = face_private_key_path(), face_public_key_path()
    if priv_path.exists() and pub_path.exists():
        return
    key = Ed25519PrivateKey.generate()
    priv_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    pub_path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))


def sign_evidence(payload: dict) -> str:
    key = serialization.load_pem_private_key(face_private_key_path().read_bytes(), password=None)
    body = b64e(_canonical(payload))
    return body + "." + b64e(key.sign(body.encode()))


def verify_evidence(token: str, *, expected_session: str, expected_user: str) -> dict:
    try:
        body, sig = token.split(".")
        public: Ed25519PublicKey = serialization.load_pem_public_key(face_public_key_path().read_bytes())
        public.verify(b64d(sig), body.encode())
        payload = json.loads(b64d(body))
    except (ValueError, InvalidSignature, FileNotFoundError) as exc:
        raise TokenError(f"Firma de evidencia inválida: {exc.__class__.__name__}") from exc
    if payload.get("iss") != "face-service" or payload.get("aud") != "labbot-backend":
        raise TokenError("Emisor o audiencia inválidos")
    if payload.get("exp", 0) < time.time():
        raise TokenError("Evidencia vencida")
    if payload.get("sid") != expected_session:
        raise TokenError("La evidencia pertenece a otra sesión")
    if payload.get("sub") != expected_user:
        raise TokenError("La evidencia corresponde a otra identidad")
    if payload.get("liveness") != "aprobada":
        raise TokenError("La comprobación de presencia no fue aprobada")
    return payload


# --- HMAC (contexto MCP y tickets) ------------------------------------------
def _hmac_sign(payload: dict, secret_name: str) -> str:
    body = b64e(_canonical(payload))
    mac = hmac.new(config.secret(secret_name).encode(), body.encode(), hashlib.sha256).digest()
    return body + "." + b64e(mac)


def _hmac_verify(token: str, secret_name: str) -> dict:
    try:
        body, mac = token.split(".")
    except (ValueError, AttributeError) as exc:
        raise TokenError("Token mal formado") from exc
    expected = hmac.new(config.secret(secret_name).encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, b64d(mac)):
            raise TokenError("Firma inválida")
        payload = json.loads(b64d(body))
    except ValueError as exc:  # base64 o JSON inválidos
        raise TokenError("Token mal formado") from exc
    if payload.get("exp", 0) < time.time():
        raise TokenError("Token vencido")
    return payload


def sign_context(*, user_id: str, role: str, session_id: str, request_id: str, authenticated: bool, ttl: int = 60) -> str:
    return _hmac_sign({"sub": user_id, "role": role, "sid": session_id, "rid": request_id,
                       "auth": authenticated, "exp": time.time() + ttl}, "MCP_SHARED_SECRET")


def verify_context(token: str) -> dict:
    return _hmac_verify(token, "MCP_SHARED_SECRET")


def sign_enroll_ticket(*, user_id: str, purpose: str, ttl: int = 300) -> str:
    return _hmac_sign({"sub": user_id, "purpose": purpose, "exp": time.time() + ttl, "nonce": secrets.token_hex(8)},
                      "ENROLL_TICKET_SECRET")


def verify_enroll_ticket(token: str, purpose: str) -> dict:
    payload = _hmac_verify(token, "ENROLL_TICKET_SECRET")
    if payload.get("purpose") != purpose:
        raise TokenError("Ticket emitido para otro propósito")
    return payload


# --- Sesiones --------------------------------------------------------------
def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- TOTP (RFC 6238) ---------------------------------------------------------
def totp(secret_b32: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((at if at is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def verify_totp(secret_b32: str, code: str, window: int = 1) -> bool:
    now = time.time()
    return any(hmac.compare_digest(totp(secret_b32, now + i * 30), code.strip()) for i in range(-window, window + 1))
