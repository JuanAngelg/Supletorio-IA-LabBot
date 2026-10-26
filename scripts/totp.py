"""Simula la aplicación autenticadora de una cuenta de PRUEBA (mecanismo alternativo).

Uso:  python scripts/totp.py est-001
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labbot import db, security  # noqa: E402
from labbot.domain import service  # noqa: E402

if len(sys.argv) != 2:
    sys.exit("Uso: python scripts/totp.py <usuario_id>")
with db.business() as conn:
    secret = service.get_user(conn, sys.argv[1])["totp_secret"]
print(f"Código para {sys.argv[1]}: {security.totp(secret)} (válido ~{30 - int(time.time()) % 30} s)")
