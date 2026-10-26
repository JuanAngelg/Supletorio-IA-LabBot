# Autenticación facial y tratamiento de datos biométricos

## 1. Flujo
1. **Identidad declarada.** La persona elige su cuenta al crear la sesión. Hasta verificarla,
   solo puede consultar reglamentos y disponibilidad.
2. **Verificación 1:1.** El backend pide al servicio facial un desafío para *ese* usuario y
   *esa* sesión (`POST /v1/verifications`, con clave de servicio). Se compara contra la plantilla
   del usuario declarado; nunca se busca en toda la base (no es identificación 1:N).
3. **Comprobación de presencia.** Secuencia aleatoria de 5 poses que empieza en "frente"
   (p. ej. frente → izquierda → frente → derecha → izquierda). El navegador captura 3 fotogramas
   por paso y los envía **directamente** al servicio facial.
4. **Evidencia verificable.** Si todo pasa, el servicio firma con **Ed25519** un token
   `{iss, aud, sub, sid, vid, amr, liveness, iat, exp, jti}`. El backend lo obtiene una sola vez
   (servidor a servidor), verifica firma, audiencia, vencimiento (≤ 15 min), que `sid` sea la
   sesión actual, que `sub` sea la identidad declarada y que el `jti` no se haya usado.
5. **El LLM no participa.** Solo ve "captura iniciada" o "verificada/no verificada". El estado
   está en `sessions.auth_method/auth_expires_at` y únicamente lo escribe `auth.bind_auth`
   tras la verificación criptográfica. El filtro de veracidad corrige al LLM si afirma lo contrario.
6. **Alternativa.** Tras 3 fallos en 15 minutos el servicio bloquea la verificación facial y
   se ofrece el **código TOTP** (RFC 6238) en un formulario seguro (nunca en el chat); 5 códigos
   erróneos bloquean la sesión. Quien no quiera enrolarse usa siempre esta vía.
7. **Separación identidad/permiso.** La verificación solo prueba quién es la persona; qué
   puede hacer lo decide `policy.py` y el dominio.

## 2. Comprobaciones de presencia y suplantación

| Comprobación | Ataque que mitiga |
|---|---|
| Un solo rostro por fotograma | Otra persona en cuadro, collage |
| Pose correcta en cada paso (mayoría de fotogramas) | Foto impresa o en pantalla: al inclinar una foto plana, el cociente nariz/ojos casi no cambia |
| Rango total de giro ≥ 0,35 | Fotos inclinadas, recortes |
| Fotogramas distintos entre pasos | Inyección de una misma imagen fija |
| Secuencia aleatoria por intento y de un solo uso | Video pregrabado (debe coincidir con la secuencia exacta) |
| Misma persona en todos los fotogramas (similitud ≥ 0,25) | Cambiar de cara entre pasos |
| Coincidencia frontal con la plantilla (SFace, coseno ≥ 0,363) | Impostor |
| Vencimiento de 120 s del desafío | Reproducción diferida |

**No cubre:** deepfakes o avatares animados en tiempo real que sigan las instrucciones,
máscaras 3D, cámaras virtuales manipuladas por un atacante con acceso al equipo, ni ataques
a la propia red neuronal. Para eso harían falta detectores PAD entrenados (textura, profundidad,
IR) certificados según ISO/IEC 30107-3. El proyecto no controla accesos físicos reales.

## 3. Almacenamiento, protección y eliminación

| Dato | Dónde | Protección | Retención / eliminación |
|---|---|---|---|
| Imágenes y fotogramas | Solo en memoria del servicio facial durante la petición | No se escriben en disco ni en logs; tamaño máx. 800 KB por fotograma | Se descartan al responder |
| Plantilla (vector de 128 floats) | `var/face.db`, tabla `templates` | Cifrada con **Fernet** (AES-128-CBC + HMAC-SHA256) con clave exclusiva del servicio (`var/keys/face_template.key` o `FACE_TEMPLATE_KEY`) | Máx. 180 días (`retention_until`); purga al arrancar; eliminación a pedido con `DELETE` + `VACUUM` |
| Consentimiento | `face.db`, `consent_log` | Sin datos biométricos: usuario, acción, versión, fecha | Se conserva como evidencia de consentimiento/eliminación |
| Verificaciones | `face.db`, `verifications` | Id aleatorio, estado y motivo; la evidencia se borra al entregarse | — |
| Auditoría general | `var/audit.db` | `sanitize()` elimina `frames`, `image`, `template`, `embedding`, `evidence_token`, `token`, `ticket`, `totp*` | Solo registra método, resultado, id de verificación, `jti` y vencimiento |
| LLM | — | Nunca recibe imágenes, plantillas, tokens ni códigos | — |

El enrolamiento exige: sesión ya autenticada (código alternativo), **ticket** HMAC de 5 minutos
para ese usuario y propósito, casilla de **consentimiento** con el texto de POL-BIO-001 y al
menos 3 fotogramas frontales nítidos y consistentes. La eliminación ("Eliminar mis datos
biométricos") exige un ticket de propósito `eliminar` del mismo usuario.

Las claves de producción deberían vivir en un gestor de secretos o HSM y el servicio facial
correr en otro host; en el prototipo están en `var/keys/` (excluido de git).

## 4. Participantes y datos
- Solo voluntarios que firmen el consentimiento, o datos de prueba autorizados.
- `scripts/capture_dataset.py` pide confirmación de consentimiento, usa seudónimos y guarda en
  `datos_voluntarios/` (excluido de git). Borre esa carpeta al terminar la evaluación.
- Las pruebas automáticas usan un **motor simulado** (`FACE_ENGINE=simulated`) cuyas
  "imágenes" son JSON: no hay fotografías de personas en el repositorio.

## 5. Evaluación de FAR/FRR
`eval/face_eval.py` calcula falsas aceptaciones y falsos rechazos con intervalo de Wilson al 95 %:
- `dataset --dir datos_voluntarios`: comparaciones genuinas e impostoras entre voluntarios,
  más barrido de umbral.
- `intentos --csv intentos.csv`: intentos reales hechos en la interfaz durante la demo,
  etiquetados como genuino, impostor, foto o video; reporta FRR y la tasa de aceptación por tipo de ataque.

**Limitaciones de la muestra:** con N pocos participantes los intervalos son muy amplios
(p. ej., 0 errores en 15 comparaciones genuinas es compatible con un FRR real de hasta ~20 %);
las comparaciones impostoras entre las mismas personas no son independientes; la iluminación y
la cámara son las del laboratorio; los ataques los hace el propio equipo, no atacantes expertos.
