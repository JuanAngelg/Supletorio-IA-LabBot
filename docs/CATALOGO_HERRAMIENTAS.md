# Catálogo de herramientas

Generado por `scripts/gen_catalog.py` a partir de `labbot/orchestrator/tools.py` y `labbot/domain/policy.py`.

Todas las llamadas (del router o del agente) pasan por el ejecutor con controles: validación del esquema de entrada → política de roles y autenticación → (escritura) validación previa y operación pendiente de confirmación → ejecución con reintentos → validación de la salida `ToolResult` → evento `tool_call` en la auditoría.

Campos que **nunca** ve el LLM y añade el orquestador: `ctx_token` (identidad, rol y autenticación firmados con HMAC), `modo` (`validar`/`ejecutar`) e `idempotency_key`.

| Herramienta | Transporte | Escribe | Requiere identidad verificada | Roles permitidos |
|---|---|---|---|---|
| `buscar_reglamento` | **MCP** | no | no | estudiante, encargado, auditor |
| `consultar_disponibilidad` | **MCP** | no | no | estudiante, encargado, auditor |
| `consultar_mis_reservas_prestamos` | local | no | sí | estudiante, encargado, auditor |
| `crear_reserva` | **MCP** | sí (con confirmación) | sí | estudiante, encargado |
| `cancelar_reserva` | **MCP** | sí (con confirmación) | sí | estudiante, encargado |
| `registrar_prestamo` | local | sí (con confirmación) | sí | encargado |
| `registrar_devolucion` | local | sí (con confirmación) | sí | encargado |
| `iniciar_autenticacion_facial` | local | no | no | estudiante, encargado, auditor |
| `verificar_autenticacion_facial` | local | no | no | estudiante, encargado, auditor |
| `iniciar_autenticacion_alternativa` | local | no | no | estudiante, encargado, auditor |
| `consultar_auditoria` | **MCP** | no | sí | estudiante, encargado, auditor |
| `verificar_integridad_auditoria` | local | no | sí | auditor |

Herramientas expuestas por el servidor MCP propio (`mcp_server/server.py`): `buscar_reglamento`, `consultar_disponibilidad`, `crear_reserva`, `cancelar_reserva`, `consultar_auditoria`.

Reglas adicionales de alcance: un estudiante solo consulta sus propias reservas/préstamos y su propia auditoría; solo el titular (o el encargado) cancela una reserva; el auditor no tiene ninguna herramienta de escritura.

## Salida común (`ToolResult`)

`status` ∈ {ok, error, denegado, requiere_autenticacion, requiere_confirmacion, servicio_no_disponible, entrada_invalida}; `data` (resultado), `error` {code, message, articulo, category}, `mensaje`.

## Esquemas de entrada

### `buscar_reglamento`

Busca en reglamentos, manuales y procedimientos del laboratorio. Úsela para requisitos, restricciones, horarios, sanciones o uso de equipos. Devuelve fragmentos con identificador para citar.

```json
{
  "additionalProperties": false,
  "properties": {
    "consulta": {
      "description": "Pregunta o tema a buscar en reglamentos y manuales",
      "maxLength": 500,
      "minLength": 3,
      "title": "Consulta",
      "type": "string"
    },
    "max_resultados": {
      "default": 4,
      "maximum": 6,
      "minimum": 1,
      "title": "Max Resultados",
      "type": "integer"
    }
  },
  "required": [
    "consulta"
  ],
  "title": "BuscarReglamentoIn",
  "type": "object"
}
```

### `consultar_disponibilidad`

Consulta qué equipos están libres en una fecha y horario (por tipo o código).

```json
{
  "additionalProperties": false,
  "properties": {
    "tipo_equipo": {
      "anyOf": [
        {
          "enum": [
            "osciloscopio",
            "generador",
            "multimetro",
            "fuente",
            "analizador"
          ],
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "Tipo de equipo",
      "title": "Tipo Equipo"
    },
    "equipo_id": {
      "anyOf": [
        {
          "pattern": "^[A-Za-z]{3}-\\d{2}$",
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "Código del equipo, p. ej. OSC-01",
      "title": "Equipo Id"
    },
    "fecha": {
      "description": "Fecha en formato AAAA-MM-DD",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
      "title": "Fecha",
      "type": "string"
    },
    "hora_inicio": {
      "description": "Hora en formato 24 h HH:MM",
      "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$",
      "title": "Hora Inicio",
      "type": "string"
    },
    "hora_fin": {
      "description": "Hora en formato 24 h HH:MM",
      "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$",
      "title": "Hora Fin",
      "type": "string"
    }
  },
  "required": [
    "fecha",
    "hora_inicio",
    "hora_fin"
  ],
  "title": "ConsultarDisponibilidadIn",
  "type": "object"
}
```

### `consultar_mis_reservas_prestamos`

Lista las reservas y préstamos del usuario autenticado (o de otro usuario si es encargado o auditor).

```json
{
  "additionalProperties": false,
  "properties": {
    "usuario_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "Solo encargado o auditor: consultar a otro usuario",
      "title": "Usuario Id"
    },
    "incluir_historial": {
      "default": false,
      "title": "Incluir Historial",
      "type": "boolean"
    }
  },
  "title": "ConsultarMisReservasIn",
  "type": "object"
}
```

### `crear_reserva`

Prepara una reserva de un equipo a nombre del usuario autenticado. El sistema pedirá confirmación al usuario antes de registrarla.

```json
{
  "additionalProperties": false,
  "properties": {
    "equipo_id": {
      "description": "Código del equipo a reservar",
      "pattern": "^[A-Za-z]{3}-\\d{2}$",
      "title": "Equipo Id",
      "type": "string"
    },
    "fecha": {
      "description": "Fecha en formato AAAA-MM-DD",
      "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
      "title": "Fecha",
      "type": "string"
    },
    "hora_inicio": {
      "description": "Hora en formato 24 h HH:MM",
      "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$",
      "title": "Hora Inicio",
      "type": "string"
    },
    "hora_fin": {
      "description": "Hora en formato 24 h HH:MM",
      "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$",
      "title": "Hora Fin",
      "type": "string"
    }
  },
  "required": [
    "equipo_id",
    "fecha",
    "hora_inicio",
    "hora_fin"
  ],
  "title": "CrearReservaIn",
  "type": "object"
}
```

### `cancelar_reserva`

Prepara la cancelación de una reserva por su código. El sistema pedirá confirmación.

```json
{
  "additionalProperties": false,
  "properties": {
    "reserva_id": {
      "description": "Código de la reserva, p. ej. RES-0001",
      "pattern": "^[Rr][Ee][Ss]-\\d{1,6}$",
      "title": "Reserva Id",
      "type": "string"
    },
    "motivo": {
      "anyOf": [
        {
          "maxLength": 200,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Motivo"
    }
  },
  "required": [
    "reserva_id"
  ],
  "title": "CancelarReservaIn",
  "type": "object"
}
```

### `registrar_prestamo`

(Encargado) Prepara el registro del préstamo de un equipo a un estudiante. Requiere confirmación.

```json
{
  "additionalProperties": false,
  "properties": {
    "usuario_id": {
      "description": "Código del estudiante que retira el equipo",
      "maxLength": 20,
      "minLength": 3,
      "title": "Usuario Id",
      "type": "string"
    },
    "equipo_id": {
      "pattern": "^[A-Za-z]{3}-\\d{2}$",
      "title": "Equipo Id",
      "type": "string"
    },
    "reserva_id": {
      "anyOf": [
        {
          "pattern": "^[Rr][Ee][Ss]-\\d{1,6}$",
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Reserva Id"
    },
    "observaciones": {
      "anyOf": [
        {
          "maxLength": 300,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Observaciones"
    }
  },
  "required": [
    "usuario_id",
    "equipo_id"
  ],
  "title": "RegistrarPrestamoIn",
  "type": "object"
}
```

### `registrar_devolucion`

(Encargado) Prepara el registro de la devolución de un préstamo con el estado del equipo. Requiere confirmación.

```json
{
  "additionalProperties": false,
  "properties": {
    "prestamo_id": {
      "description": "Código del préstamo, p. ej. PRE-0001",
      "pattern": "^[Pp][Rr][Ee]-\\d{1,6}$",
      "title": "Prestamo Id",
      "type": "string"
    },
    "estado": {
      "enum": [
        "bueno",
        "con_observaciones",
        "danado"
      ],
      "title": "Estado",
      "type": "string"
    },
    "observaciones": {
      "anyOf": [
        {
          "maxLength": 300,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Observaciones"
    }
  },
  "required": [
    "prestamo_id",
    "estado"
  ],
  "title": "RegistrarDevolucionIn",
  "type": "object"
}
```

### `iniciar_autenticacion_facial`

Inicia la verificación facial 1:1 del usuario declarado. Abre la cámara en la interfaz; el resultado lo verifica el sistema, no el asistente.

```json
{
  "additionalProperties": false,
  "properties": {},
  "title": "SinParametrosIn",
  "type": "object"
}
```

### `verificar_autenticacion_facial`

Consulta si la verificación facial en curso fue aprobada y la vincula a la sesión.

```json
{
  "additionalProperties": false,
  "properties": {},
  "title": "SinParametrosIn",
  "type": "object"
}
```

### `iniciar_autenticacion_alternativa`

Muestra el formulario seguro del código temporal (cuando la verificación facial falla o el usuario no está enrolado).

```json
{
  "additionalProperties": false,
  "properties": {},
  "title": "SinParametrosIn",
  "type": "object"
}
```

### `consultar_auditoria`

Consulta el historial de auditoría. El auditor ve todo; otros roles solo lo propio.

```json
{
  "additionalProperties": false,
  "properties": {
    "solicitud_id": {
      "anyOf": [
        {
          "maxLength": 64,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Solicitud Id"
    },
    "sesion_id": {
      "anyOf": [
        {
          "maxLength": 64,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Sesion Id"
    },
    "usuario_id": {
      "anyOf": [
        {
          "maxLength": 20,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Usuario Id"
    },
    "tipo_evento": {
      "anyOf": [
        {
          "maxLength": 40,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Tipo Evento"
    },
    "limite": {
      "default": 20,
      "maximum": 100,
      "minimum": 1,
      "title": "Limite",
      "type": "integer"
    }
  },
  "title": "ConsultarAuditoriaIn",
  "type": "object"
}
```

### `verificar_integridad_auditoria`

(Auditor) Verifica la cadena de hashes y los anclajes del registro de auditoría.

```json
{
  "additionalProperties": false,
  "properties": {},
  "title": "SinParametrosIn",
  "type": "object"
}
```
