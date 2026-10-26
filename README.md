# LabBot — chatbot auditable para préstamos de equipos de laboratorio con autenticación facial

Asistente conversacional que consulta reglamentos con citas (RAG), revisa disponibilidad, crea y
cancela reservas y registra préstamos y devoluciones. Verifica la identidad con un servicio facial
independiente y deja una traza de auditoría verificable de cada operación.

| Documento | Contenido |
|---|---|
| [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md) | Diagrama, responsabilidades, flujos A–F |
| [docs/CATALOGO_HERRAMIENTAS.md](docs/CATALOGO_HERRAMIENTAS.md) | Herramientas, roles, MCP y esquemas |
| [docs/BIOMETRIA.md](docs/BIOMETRIA.md) | Verificación 1:1, presencia, almacenamiento y eliminación |
| [docs/AUDITORIA.md](docs/AUDITORIA.md) | Eventos, cadena HMAC, qué detecta y sus límites |
| [docs/INFORME_TECNICO.md](docs/INFORME_TECNICO.md) | Decisiones, resultados y limitaciones |
| [docs/RESULTADOS.md](docs/RESULTADOS.md) | Router vs. agente (se genera con `eval/run_eval.py`) |
| [docs/GUION_VIDEO.md](docs/GUION_VIDEO.md) | Guion de la demostración |

## Estructura

```
labbot/            backend: API, orquestador, router, agente, dominio, RAG, auditoría, UI
  orchestrator/    orquestador, ejecutor de herramientas, router, agente, prompts
  domain/          política de roles y reglas de negocio
  static/          interfaz web
mcp_server/        servidor MCP propio (5 herramientas, Streamable HTTP)
face_service/      servicio independiente de autenticación facial
data/docs/         documentos del RAG (reglamento, procedimiento, manual, normas, política, foro contaminado)
data/seed.json     usuarios, equipos, inducciones y sanciones de prueba
eval/              55 casos, evaluación router vs. agente, FAR/FRR facial
tests/             67 pruebas automatizadas
scripts/           arranque, setup, TOTP de prueba, demo, manipulación de auditoría, catálogo
results/           informes generados (evaluación, auditoría exportada, manipulación)
```

## Requisitos
- Python 3.11 o superior (probado con 3.13 en Windows 11).
- [Ollama](https://ollama.com) para el LLM local.
- Cámara web para la verificación facial.

## Instalación

```bash
pip install -r requirements.txt
```

```bash
ollama signin
```

```bash
ollama pull gemma4:31b-cloud
```

Modelo usado en los resultados: `gemma4:31b-cloud` (se ejecuta en la nube de Ollama; requiere
`ollama signin`). Con 8 GB de RAM o más puede usar uno local, por ejemplo `qwen2.5:7b`,
cambiando `LLM_MODEL` en `.env`.

```bash
python scripts/download_face_models.py
```

```bash
python scripts/setup.py
```

`setup.py` crea las bases de datos, carga los datos de prueba, genera las claves locales en
`var/keys/` e indexa los documentos en ChromaDB. Opcional: copie `.env.example` a `.env`.

## Ejecución
Windows (abre tres ventanas: servicio facial :8200, servidor MCP :8100, chatbot :8000):

```bash
powershell -ExecutionPolicy Bypass -File scripts/run_all.ps1
```

Linux/macOS: `sh scripts/run_all.sh`. Luego abra http://127.0.0.1:8000.

Por separado:

```bash
python -m uvicorn face_service.app:app --port 8200
```

```bash
python -m mcp_server.server
```

```bash
python -m uvicorn labbot.api:app --port 8000
```

### Cuentas de prueba

| Usuario | Rol | Observación |
|---|---|---|
| est-001 Ana Torres | estudiante | inducción de osciloscopio y analizador |
| est-002 Bruno Paredes | estudiante | inducción de osciloscopio |
| est-003 Carla Méndez | estudiante | sin inducción (se le rechazan osciloscopios) |
| est-004 Diego Ríos | estudiante | con sanción vigente |
| enc-001 Elena Vásquez | encargado | registra préstamos y devoluciones |
| aud-001 Fernando Lima | auditor | consulta e integridad de la auditoría |

Código alternativo (simula la app autenticadora de la cuenta de prueba):

```bash
python scripts/totp.py est-001
```

Primer uso de la verificación facial: "Código alternativo" → "Enrolar rostro" (consentimiento) →
desde entonces, "Verificar rostro" o cualquier operación que la requiera.

## Pruebas y evaluación

```bash
python -m pytest -q
```

```bash
python eval/run_eval.py --llm ollama
```

```bash
python eval/run_eval.py --llm mock
```

La segunda genera `docs/RESULTADOS.md` (comparación real). La tercera solo verifica la tubería sin
modelo y escribe `docs/RESULTADOS_SIMULADO.md`.

```bash
python scripts/demo_session.py --llm ollama
```

```bash
python scripts/tamper_demo.py
```

```bash
python eval/face_eval.py dataset --dir datos_voluntarios
```

- `demo_session.py`: sesión completa y exportación de su auditoría sin biometría en `results/`.
- `tamper_demo.py`: qué alteraciones de la auditoría se detectan y cuáles no.
- `face_eval.py`: FAR/FRR con voluntarios (capture con `scripts/capture_dataset.py`), o
  `face_eval.py intentos --csv intentos.csv` con los intentos etiquetados de la demo.

## Seguridad en una línea
El LLM solo *propone* llamadas: identidad (firma Ed25519), permisos (RBAC + propiedad), reglas del
reglamento, confirmación (botón o mensaje reconocido por el orquestador) e idempotencia se aplican
en el backend, y todo queda en un registro de solo adición encadenado con HMAC.
