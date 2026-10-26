"""Análisis determinista de mensajes en español.

Lo usan el router (respaldo de la extracción del LLM: los modelos pequeños calculan mal
fechas relativas como "mañana") y el LLM simulado de las pruebas automáticas.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta

WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
          "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
EQUIPMENT_TYPES = [
    ("osciloscopio", r"osciloscop"),
    ("analizador", r"analizador"),
    ("generador", r"generador|gener\w+ de (funciones|senales)"),
    ("multimetro", r"multimetro|tester|polimetro"),
    ("fuente", r"fuente"),
]


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def parse_date(text: str, now: datetime) -> str | None:
    t = norm(text)
    today = now.date()
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        return m.group(0)
    if m := re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        year = int(m.group(3)) if m.group(3) else today.year
        year = year + 2000 if year < 100 else year
        try:
            return date(year, int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    if m := re.search(r"\b(\d{1,2}) de (" + "|".join(MONTHS) + r")\b", t):
        d = date(today.year, MONTHS[m.group(2)], int(m.group(1)))
        if d < today:
            d = d.replace(year=today.year + 1)
        return d.isoformat()
    if "pasado manana" in t:
        return (today + timedelta(days=2)).isoformat()
    # "mañana" como día, descartando "en/de/por/esta la mañana" (franja horaria)
    if re.search(r"\bmanana\b", re.sub(r"\b(en|de|por|esta)( la)? manana\b", " ", t)):
        return (today + timedelta(days=1)).isoformat()
    if re.search(r"\bhoy\b", t):
        return today.isoformat()
    for name, wd in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", t):
            delta = (wd - today.weekday()) % 7
            delta = 7 if delta == 0 else delta
            return (today + timedelta(days=delta)).isoformat()
    return None


_TIME = r"(\d{1,2})(?:[:h\.](\d{2}))?"
_PM = r"(p\.?\s?m\.?|pm|de la tarde|de la noche)"
_AM = r"(a\.?\s?m\.?|am|de la manana)"


def _to24(h: int, mi: int, pm: bool, am: bool) -> tuple[int, int]:
    if pm and h < 12:
        h += 12
    elif not am and not pm and 1 <= h <= 6:
        h += 12  # "de 2 a 4" en un laboratorio significa de la tarde
    return h, mi


def parse_times(text: str) -> tuple[str | None, str | None]:
    t = norm(text)
    m = re.search(rf"(?:de|desde|entre)\s+(?:las\s+)?{_TIME}\s*(?:{_AM}|{_PM})?\s*(?:a|hasta|y)\s+(?:las\s+)?{_TIME}\s*(?:{_AM}|{_PM})?", t)
    if m:
        h1, m1 = int(m.group(1)), int(m.group(2) or 0)
        am1, pm1 = bool(m.group(3)), bool(m.group(4))
        h2, m2 = int(m.group(5)), int(m.group(6) or 0)
        am2, pm2 = bool(m.group(7)), bool(m.group(8))
        if not am1 and not pm1:
            if pm2:
                # "de 2 a 4 pm" -> ambas de la tarde; "de 11 a 1 pm" -> 11:00 a 13:00
                pm1, am1 = (True, False) if h1 <= h2 else (False, True)
            elif am2:
                am1 = True
        h1, m1 = _to24(h1, m1, pm1, am1)
        h2, m2 = _to24(h2, m2, pm2, am2)
        if h2 * 60 + m2 <= h1 * 60 + m1 and h2 < 12:
            h2 += 12
        if 0 <= h1 < 24 and 0 <= h2 < 24:
            return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"
    m = re.search(rf"a las\s+{_TIME}\s*(?:{_AM}|{_PM})?(?:.*?\bpor\s+(\d+)\s*hora)?", t)
    if m:
        h, mi = _to24(int(m.group(1)), int(m.group(2) or 0), bool(m.group(4)), bool(m.group(3)))
        dur = int(m.group(5) or 2)
        if h < 24:
            return f"{h:02d}:{mi:02d}", f"{min(h + dur, 23):02d}:{mi:02d}"
    return None, None


def equipment_type(text: str) -> str | None:
    t = norm(text)
    for name, pattern in EQUIPMENT_TYPES:
        if re.search(pattern, t):
            return name
    return None


def find_code(text: str, prefix: str) -> str | None:
    m = re.search(rf"\b{prefix}-?(\d{{1,6}})\b", text, re.IGNORECASE)
    if not m:
        return None
    if prefix.upper() in ("RES", "PRE"):
        return f"{prefix.upper()}-{int(m.group(1)):04d}"
    return f"{prefix.upper()}-{int(m.group(1)):02d}"


def equipment_id(text: str) -> str | None:
    m = re.search(r"\b(OSC|GEN|MUL|FUE|ANA)-?(\d{2})\b", text, re.IGNORECASE)
    return f"{m.group(1).upper()}-{m.group(2)}" if m else None


def user_id(text: str) -> str | None:
    m = re.search(r"\b(est|enc|aud)-(\d{3})\b", text, re.IGNORECASE)
    return f"{m.group(1).lower()}-{m.group(2)}" if m else None


def return_state(text: str) -> str | None:
    t = norm(text)
    if re.search(r"\bdanad|\broto\b|averiad", t):
        return "danado"
    if "observacion" in t:
        return "con_observaciones"
    if re.search(r"\bbuen(o|as)?\b|\bbien\b|sin novedad", t):
        return "bueno"
    return None


def classify(text: str) -> str:
    t = norm(text)
    if re.search(r"auditor|integridad|trazabilidad|registro de (eventos|auditoria)|\breq_[0-9a-f]+", t):
        return "auditoria"
    # Preguntas sobre normas aunque mencionen operaciones ("¿quién puede anular…?", "¿qué pasa si…?")
    if re.search(r"^\W*(que pasa|que hago|como se|cual es|cuant[ao]s?|quien puede|se guardan|segun el reglamento|"
                 r"que dice|que sancion|que equipo de proteccion|que requisitos)\b", t):
        return "consulta_reglamento"
    if re.search(r"devoluci|devolver|devuelv|regres\w+ (el|la) equipo", t) and not re.search(r"requisit|que pasa|sancion", t):
        return "registrar_devolucion"
    if re.search(r"registr\w*\s+(el\s+|un\s+)?prestamo|prest\w+\s+(el\s+|la\s+)?\w+-\d+\s+a|entreg\w+\s+(el|la)\s+\w+-\d+", t):
        return "registrar_prestamo"
    if re.search(r"\bcancel|\banul|elimin\w+ (la|mi) reserva", t):
        return "cancelar_reserva"
    if re.search(r"\b(mis|tengo|ver)\b.*\b(reservas|prestamos)\b|\b(reservas|prestamos)\b.*\b(tengo|mias|activ)|"
                 r"reservas (de|del)\s+(est|enc)-|prestamos (de|del)\s+(est|enc)-", t) \
            and not re.search(r"\bpuedo\b|maxim|permit|regla", t):
        return "mis_reservas"
    if re.search(r"autentic|verific\w+ (mi )?identidad|reconocimiento facial|codigo (alternativo|temporal)|verificacion facial", t) \
            and not re.search(r"reserv|prest|cancel", t):
        return "autenticacion"
    if re.search(r"\breserv[ae]\w*|\bapart[ae]\w*", t) and not re.search(r"requisit|cuant\w+ reservas|puedo tener|maxim|regla", t):
        return "crear_reserva"
    if re.search(r"disponib|libre|hay (algun|un|una)\b.*(osciloscop|multimetro|fuente|generador|analizador)", t):
        return "consultar_disponibilidad"
    if re.search(r"requisit|reglament|norma|manual|sancion|horario|puedo|debo|cuant|como|que pasa|que hago|"
                 r"permit|prohib|biometr|datos|seguridad|voltaje|sonda|\?", t):
        return "consulta_reglamento"
    return "general"


def extract(text: str, now: datetime) -> dict:
    start, end = parse_times(text)
    return {
        "tipo_equipo": equipment_type(text),
        "equipo_id": equipment_id(text),
        "fecha": parse_date(text, now),
        "hora_inicio": start,
        "hora_fin": end,
        "reserva_id": find_code(text, "RES"),
        "prestamo_id": find_code(text, "PRE"),
        "usuario_id": user_id(text),
        "estado": return_state(text),
        "solicitud_id": (m.group(0) if (m := re.search(r"\breq_[0-9a-f]{8,}\b", text)) else None),
        "alternativa": bool(re.search(r"codigo|alternativ|sin camara|no tengo camara", norm(text))),
        "integridad": "integridad" in norm(text) or "alterac" in norm(text),
    }
