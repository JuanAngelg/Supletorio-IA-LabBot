"""Detección de intentos de manipulación (prompt injection).

Es una capa de *detección*, no la defensa principal: las reglas de acceso viven en el
backend (policy + dominio + evidencias firmadas) y se cumplen aunque esta capa falle.
Se usa para:
- poner en cuarentena fragmentos recuperados por el RAG que contienen instrucciones
  dirigidas al asistente (no se entregan al LLM);
- marcar en la auditoría los mensajes de usuario con patrones de manipulación.
"""
from __future__ import annotations

import re
import unicodedata

PATTERNS = [
    (r"ignor[ae]\w*\s+(todas?\s+)?(las\s+)?(instrucciones|reglas|indicaciones)", "ignorar_instrucciones"),
    (r"(omit|salt|evit|desactiv|deshabilit)\w*\s+(la\s+|el\s+)?(autenticaci|verificaci|confirmaci|reconocimiento)", "omitir_controles"),
    (r"sin\s+(pedir\s+|solicitar\s+)?(autenticaci|verificaci|confirmaci)", "omitir_controles"),
    (r"(ya\s+est[aá]n?|est[aá]n?|declara\w*\s+que\s+(el\s+usuario\s+)?est[aá])\s+(verificad|autenticad)", "declarar_autenticado"),
    (r"(para|al)\s+el\s+asistente\s+de\s+ia|instrucci[oó]n\s+(para|al)\s+(el\s+)?(asistente|modelo|sistema)", "instruccion_al_asistente"),
    (r"(eres|act[uú]a\s+como)\s+(ahora\s+)?(un\s+)?(administrador|admin|encargado|auditor|root|desarrollador)", "suplantar_rol"),
    (r"(modo|rol)\s+(administrador|desarrollador|dios|debug)", "suplantar_rol"),
    (r"system\s*prompt|prompt\s+del\s+sistema|tus\s+instrucciones\s+internas", "extraer_prompt"),
    (r"ejecut\w*\s+(directamente\s+)?(la\s+)?herramienta", "ordenar_herramienta"),
    (r"\b(ignore|disregard)\s+(all\s+)?(previous|prior)\s+instructions", "ignorar_instrucciones"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in PATTERNS]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text)


def scan(text: str) -> list[str]:
    """Devuelve las etiquetas de los patrones detectados (lista vacía si no hay)."""
    norm = _normalize(text or "")
    found = []
    for regex, label in _COMPILED:
        if regex.search(norm) and label not in found:
            found.append(label)
    return found
