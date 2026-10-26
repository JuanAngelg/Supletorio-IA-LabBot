"""Prepara el entorno local: esquemas, datos de prueba, claves e índice vectorial.

Uso:  python scripts/setup.py [--reset]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labbot import audit, config, db, faults, rag, security  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="borra reservas, préstamos y sesiones")
    args = parser.parse_args()
    db.init_schema()
    audit.init()
    with db.business() as conn:
        empty = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    if empty or args.reset:
        db.reset_business_data()
        db.reset_app_data()
        print("Datos de prueba cargados desde data/seed.json")
    faults.clear()
    security.ensure_face_keypair()
    for name in ("AUDIT_HMAC_KEY", "MCP_SHARED_SECRET", "ENROLL_TICKET_SECRET", "FACE_SERVICE_KEY"):
        config.secret(name)
    print("Claves listas en", config.keys_dir())
    print("Índice vectorial:", rag.ingest())


if __name__ == "__main__":
    main()
