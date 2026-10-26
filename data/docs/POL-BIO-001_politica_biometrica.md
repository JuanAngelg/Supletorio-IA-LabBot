---
id: POL-BIO-001
titulo: Política de tratamiento de datos biométricos del asistente del laboratorio
version: 1.0
vigencia: 2026-09-01
confianza: oficial
---

# Política de datos biométricos

## Sección 1. Finalidad y participación voluntaria
La verificación facial se usa únicamente para confirmar que quien opera el asistente es la persona que declaró su identidad (verificación uno a uno). La participación es voluntaria: quien no otorgue su consentimiento usa el código temporal alternativo sin ninguna desventaja. En este proyecto solo participan voluntarios o datos de prueba autorizados.

## Sección 2. Consentimiento
Antes del enrolamiento se muestra el texto de consentimiento con la finalidad, el tiempo de conservación y el derecho de eliminación. El consentimiento se registra con su versión, fecha y hora. Puede revocarse en cualquier momento, lo que elimina la plantilla.

## Sección 3. Datos almacenados
No se almacenan fotografías ni videos. Las imágenes se procesan en memoria en el servicio de verificación facial y se descartan. Solo se guarda una plantilla numérica (vector de 128 valores) cifrada con AES (Fernet) usando una clave exclusiva del servicio facial. Las imágenes y plantillas nunca se envían al modelo de lenguaje ni a los registros generales de auditoría.

## Sección 4. Conservación y eliminación
La plantilla se conserva como máximo 180 días o hasta el fin del periodo académico, lo que ocurra primero. Se elimina de forma definitiva cuando el titular revoca el consentimiento, cuando vence el plazo, o a solicitud del encargado. La eliminación queda registrada sin incluir datos biométricos.

## Sección 5. Comprobación de presencia
Para evitar suplantaciones con fotografías o videos, el sistema pide al usuario realizar una secuencia aleatoria de movimientos de cabeza. Tras tres intentos fallidos se bloquea temporalmente la verificación facial y se ofrece el código alternativo.
