#!/usr/bin/env sh
# Arranca los tres procesos en segundo plano (Linux/macOS). Ctrl+C detiene todo.
cd "$(dirname "$0")/.." || exit 1
python scripts/setup.py
python -m uvicorn face_service.app:app --host 127.0.0.1 --port 8200 &
python -m mcp_server.server &
python -m uvicorn labbot.api:app --host 127.0.0.1 --port 8000 &
echo "Abra http://127.0.0.1:8000"
trap 'kill 0' INT TERM
wait
