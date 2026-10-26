# Auditoría

## 1. Qué se registra
Cada mensaje o confirmación recibe un `request_id` (`req_…`). Eventos por solicitud:

| Requisito del enunciado | Evento(s) y campos |
|---|---|
| Usuario, sesión, fecha y hora | columnas `user_id`, `session_id`, `ts` (ms) en todos los eventos |
| Solicitud recibida | `request_received.solicitud`, estrategia, rol, estado de autenticación |
| Ruta seleccionada | `route_selected` (ruta, confianza, fuente: llm / palabras_clave / agente) |
| Herramientas invocadas | `tool_call` (herramienta, transporte local/MCP, argumentos validados, autorización, intentos, estado, código, duración); `mcp_tool_executed` desde el servidor MCP |
| Evidencias documentales | `rag_evidence` (id, score, versión, cuarentena, patrones); `final_result.evidencias`, `citas_validas`, `citas_invalidas` |
| Autenticación y autorización | `auth_face_started`, `auth_result` (método, resultado, motivo, jti, vencimiento); `authz_denied`, `authz_decision` |
| Confirmación del usuario | `confirmation_requested` (op, resumen, clave de idempotencia); `confirmation_decision` (confirmar/rechazar, vía botón o mensaje) |
| Cambios realizados y resultado final | `db_change` (tabla, id, campo, antes → después, si fue repetida/reconciliada); `final_result` (estado, respuesta, cambios) |
| Errores, reintentos, tiempos | `error`, `retry` (intento, servicio), `reconciliation`, `truth_guard`; `final_result.latencia_ms`, `llm_ms`, tokens, costo; `llm_call` por llamada |

No se guardan razonamientos internos del modelo: solo decisiones operativas (ruta, herramientas
pedidas, resultados). Nunca se guardan imágenes, plantillas, tokens ni códigos (`audit.sanitize`).

**Completitud:** `audit.request_completeness()` exige `request_received`, `route_selected`
(o `confirmation_decision`), `final_result` con estado y latencia, y que cada `tool_call` tenga
decisión de autorización y duración. La evaluación la mide para cada solicitud.

## 2. Solo adición y detección de alteraciones
- **Triggers** `BEFORE UPDATE/DELETE → RAISE(ABORT)`: la aplicación no puede modificar ni borrar.
- **Cadena HMAC-SHA256:** `hash = HMAC(K, prev_hash ‖ JSON canónico{seq, ts, request_id, session_id, user_id, event_type, data})`.
  La clave `AUDIT_HMAC_KEY` no está en la BD.
- **Anclajes externos:** al cerrar cada solicitud se añade `(seq, hash)` a
  `var/audit_anchors.jsonl` (en producción: almacenamiento WORM u otro sistema, p. ej. un bucket
  con *object lock* o un servicio de sellado de tiempo).
- **Verificación:** `audit.verify()` recalcula la cadena, comprueba la continuidad de `seq` y
  contrasta cada anclaje. Disponible para el auditor en la interfaz ("Verificar integridad"),
  como herramienta `verificar_integridad_auditoria` y en `scripts/tamper_demo.py`.

## 3. Qué detecta y qué no (resultado de `scripts/tamper_demo.py`)

| Ataque (con acceso directo al archivo SQLite) | Resultado | Cómo se detecta |
|---|---|---|
| `UPDATE` desde la aplicación | **Bloqueado** | trigger de solo adición |
| Modificar un campo de un evento | **Detectado** | `contenido_alterado` (HMAC no coincide) |
| Borrar un evento intermedio | **Detectado** | `secuencia_discontinua` + `enlace_roto` |
| Reordenar eventos | **Detectado** | `contenido_alterado` |
| Insertar un evento falso sin la clave | **Detectado** | `contenido_alterado` |
| Truncar el final ya anclado | **Detectado** | `truncamiento` frente al anclaje |
| Reescribir toda la cadena **con** la clave HMAC | **Detectado** | `anclaje_no_coincide` |
| Truncar el final **y** borrar el último anclaje | No detectado | límite |
| Reescribir la cadena con la clave **y** los anclajes | No detectado | límite |

**Límites:**
1. Quien controla a la vez la clave HMAC, la BD y el almacén de anclajes puede rehacer la
   historia. Mitigación: anclajes en un sistema administrado por otra persona (separación de
   funciones), firma con clave en HSM, sellado de tiempo externo.
2. Los eventos posteriores al último anclaje se pueden truncar sin detección si también se
   altera el anclaje; la ventana es de una solicitud porque se ancla al cerrar cada una.
3. Lo que nunca se escribió no se puede detectar (p. ej., un componente modificado que no
   registra). La completitud por solicitud y el registro cruzado del servidor MCP
   (`mcp_tool_executed`) reducen ese riesgo, pero no lo eliminan.
4. La cadena prueba integridad, no veracidad: un evento falso escrito por la propia aplicación
   comprometida en el momento sería "íntegro".
5. SQLite con triggers no es inmutable frente al administrador del sistema de archivos; por eso
   la garantía real la dan el HMAC y los anclajes, no los triggers.

## 4. Exportación
- Interfaz: "Exportar auditoría de la sesión"; API: `GET /api/audit/session/{id}/export`
  (titular de la sesión o auditor autenticado).
- `scripts/demo_session.py` genera `results/auditoria_sesion_demo_<llm>.json`: una sesión completa
  agrupada por solicitud, con completitud, verificación de integridad, la conversación y el estado
  final de la BD. El script comprueba que no contiene datos biométricos.
