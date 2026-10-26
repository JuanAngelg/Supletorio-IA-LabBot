# Arranca los tres procesos en ventanas separadas (Windows PowerShell).
#   Servicio facial  -> http://127.0.0.1:8200
#   Servidor MCP     -> http://127.0.0.1:8100/mcp
#   Chatbot + UI     -> http://127.0.0.1:8000
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
python scripts/setup.py
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; python -m uvicorn face_service.app:app --host 127.0.0.1 --port 8200"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; python -m mcp_server.server"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root'; python -m uvicorn labbot.api:app --host 127.0.0.1 --port 8000"
Write-Host "Abra http://127.0.0.1:8000 en el navegador."
