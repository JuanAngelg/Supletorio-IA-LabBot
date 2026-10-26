"""Genera docs/CATALOGO_HERRAMIENTAS.md desde el registro de herramientas y la política.

Uso:  python scripts/gen_catalog.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from labbot.domain import policy  # noqa: E402
from labbot.orchestrator.tools import CATALOG  # noqa: E402

lines = [
    "# Catálogo de herramientas", "",
    "Generado por `scripts/gen_catalog.py` a partir de `labbot/orchestrator/tools.py` y `labbot/domain/policy.py`.", "",
    "Todas las llamadas (del router o del agente) pasan por el ejecutor con controles: validación del esquema de "
    "entrada → política de roles y autenticación → (escritura) validación previa y operación pendiente de "
    "confirmación → ejecución con reintentos → validación de la salida `ToolResult` → evento `tool_call` en la auditoría.", "",
    "Campos que **nunca** ve el LLM y añade el orquestador: `ctx_token` (identidad, rol y autenticación firmados con HMAC), "
    "`modo` (`validar`/`ejecutar`) e `idempotency_key`.", "",
    "| Herramienta | Transporte | Escribe | Requiere identidad verificada | Roles permitidos |",
    "|---|---|---|---|---|",
]
for spec in CATALOG.values():
    roles = ", ".join(r for r in policy.ROLES if spec.name in policy.PERMISSIONS[r])
    lines.append(f"| `{spec.name}` | {'**MCP**' if spec.transport == 'mcp' else 'local'} | "
                 f"{'sí (con confirmación)' if spec.mutating else 'no'} | {'sí' if spec.name in policy.REQUIRES_AUTH else 'no'} | {roles} |")
lines += ["", f"Herramientas expuestas por el servidor MCP propio (`mcp_server/server.py`): "
          f"{', '.join('`' + s.name + '`' for s in CATALOG.values() if s.transport == 'mcp')}.", "",
          "Reglas adicionales de alcance: un estudiante solo consulta sus propias reservas/préstamos y su propia "
          "auditoría; solo el titular (o el encargado) cancela una reserva; el auditor no tiene ninguna herramienta de escritura.", "",
          "## Salida común (`ToolResult`)", "",
          "`status` ∈ {ok, error, denegado, requiere_autenticacion, requiere_confirmacion, servicio_no_disponible, "
          "entrada_invalida}; `data` (resultado), `error` {code, message, articulo, category}, `mensaje`.", "",
          "## Esquemas de entrada", ""]
for spec in CATALOG.values():
    schema = spec.input_model.model_json_schema()
    lines += [f"### `{spec.name}`", "", spec.description, "", "```json", json.dumps(schema, indent=2, ensure_ascii=False), "```", ""]
out = ROOT / "docs" / "CATALOGO_HERRAMIENTAS.md"
out.parent.mkdir(exist_ok=True)
out.write_text("\n".join(lines), encoding="utf-8")
print("Escrito", out)
