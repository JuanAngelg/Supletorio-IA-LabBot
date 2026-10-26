---
id: PROC-LAB-002
titulo: Procedimiento de reserva, préstamo y devolución mediante el asistente
version: 1.3
vigencia: 2026-09-01
confianza: oficial
---

# Procedimiento de reserva, préstamo y devolución

## Sección 1. Identificación del usuario
El usuario declara su identidad al iniciar sesión en el asistente. Antes de consultar sus datos o realizar cualquier operación, el sistema verifica que el rostro corresponda a la identidad declarada (verificación uno a uno). La verificación tiene una vigencia de 15 minutos y está asociada a la sesión. El asistente nunca puede dar por autenticada a una persona por sí mismo.

## Sección 2. Reserva de un equipo
1. El usuario indica el tipo de equipo, la fecha y el horario.
2. El asistente consulta la disponibilidad y propone un equipo libre.
3. El sistema verifica la identidad y los permisos del usuario y valida las reglas del artículo 4 del reglamento.
4. El asistente presenta un resumen con el equipo, la fecha y el horario.
5. El usuario confirma expresamente; solo entonces se registra la reserva y se informa su código (por ejemplo, RES-0001).

## Sección 3. Cancelación
El titular solicita la cancelación indicando el código de la reserva. El sistema verifica que la reserva le pertenezca y que no haya comenzado, presenta un resumen y solicita confirmación.

## Sección 4. Retiro del equipo (préstamo)
El estudiante se presenta ante el encargado con su reserva vigente. El encargado, autenticado en el asistente, registra el préstamo indicando el código del estudiante y del equipo. El sistema comprueba los requisitos del reglamento y solicita la confirmación del encargado.

## Sección 5. Devolución
El encargado registra la devolución indicando el código del préstamo y el estado del equipo (bueno, con observaciones o dañado). El sistema calcula si hubo retraso y aplica las sanciones del artículo 8 cuando corresponda.

## Sección 6. Fallos del sistema
Si un servicio no responde, el asistente informa el estado real de la operación y no confirma operaciones que no se hayan completado. Los reintentos usan la misma clave de operación, por lo que nunca se registran reservas o préstamos duplicados.

## Sección 7. Verificación alternativa
Si la verificación facial falla tres veces, o si el usuario no desea usarla, puede verificar su identidad con el código temporal de seis dígitos de su aplicación de autenticación registrada. El código se introduce en el formulario seguro del sistema, nunca en el chat.
