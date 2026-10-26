"""Verificación 1:1 con comprobación de presencia por desafío-respuesta.

El servicio genera una secuencia aleatoria de poses (frente, girar a la izquierda, girar
a la derecha). El navegador captura varios fotogramas por paso y los envía directamente
al servicio facial. Se aprueba solo si TODAS las comprobaciones pasan:

1. Exactamente un rostro en cada fotograma válido.
2. La pose de cada paso coincide con la pedida (mayoría de fotogramas del paso).
3. El rostro frontal coincide con la plantilla enrolada (umbral SFace 0.363).
4. Todos los fotogramas son de la misma persona (umbral de consistencia).
5. El rango de giro total es suficiente: una foto impresa inclinada no cambia el yaw.
6. No hay fotogramas idénticos repetidos entre pasos (inyección de una imagen fija).
7. Nitidez mínima.

Ataques que esto mitiga: foto impresa o en pantalla (no puede girar la cabeza), imagen
fija inyectada, video pregrabado (la secuencia es aleatoria: probabilidad 1/N de coincidir).
No mitiga: deepfakes en tiempo real, máscaras 3D, ni a un atacante que controle la
cámara virtual con un modelo animado (ver docs/BIOMETRIA.md).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

import numpy as np

from .engine import Observation, cosine

YAW_SIGN = float(os.environ.get("FACE_YAW_SIGN", "1"))  # invierte si la cámara entrega imagen espejada
FRONT_MAX = 0.12
TURN_MIN = 0.20
MIN_YAW_RANGE = 0.35
CONSISTENCY_THRESHOLD = 0.25
MIN_SHARPNESS = float(os.environ.get("FACE_MIN_SHARPNESS", "15"))
STEPS = ("frente", "izquierda", "derecha")


def new_challenge(length: int = 4) -> list[str]:
    """'frente' + secuencia aleatoria sin repeticiones consecutivas que incluye ambos giros."""
    rng = secrets.SystemRandom()
    while True:
        seq = ["frente"]
        while len(seq) < length + 1:
            options = [s for s in STEPS if s != seq[-1]]
            seq.append(rng.choice(options))
        if "izquierda" in seq and "derecha" in seq:
            return seq


def pose_matches(step: str, yaw: float) -> bool:
    yaw *= YAW_SIGN
    if step == "frente":
        return abs(yaw) <= FRONT_MAX
    if step == "izquierda":
        return yaw >= TURN_MIN
    if step == "derecha":
        return yaw <= -TURN_MIN
    return False


@dataclass
class LivenessResult:
    verified: bool
    reason: str
    checks: dict = field(default_factory=dict)
    similarity: float = 0.0


def evaluate(challenge: list[str], observations: dict[int, list[Observation]], template: np.ndarray,
             match_threshold: float) -> LivenessResult:
    checks: dict[str, bool] = {}
    all_obs = [o for i in range(len(challenge)) for o in observations.get(i, [])]
    if not all_obs:
        return LivenessResult(False, "sin_fotogramas", {"fotogramas": False})

    if any(o.n_faces > 1 for o in all_obs):
        return LivenessResult(False, "multiples_rostros", {"un_rostro": False})
    valid = {i: [o for o in observations.get(i, []) if o.n_faces == 1] for i in range(len(challenge))}
    checks["un_rostro"] = all(valid[i] for i in range(len(challenge)))
    if not checks["un_rostro"]:
        return LivenessResult(False, "rostro_no_detectado", checks)

    checks["nitidez"] = all(o.sharpness >= MIN_SHARPNESS for i in valid for o in valid[i])

    pose_ok = []
    for i, step in enumerate(challenge):
        hits = sum(pose_matches(step, o.yaw) for o in valid[i])
        pose_ok.append(hits * 2 > len(valid[i]))
    checks["poses"] = all(pose_ok)

    yaws = [o.yaw for i in valid for o in valid[i]]
    checks["rango_giro"] = (max(yaws) - min(yaws)) >= MIN_YAW_RANGE

    frontal = [o for i, step in enumerate(challenge) if step == "frente" for o in valid[i]]
    sims_front = [cosine(o.embedding, template) for o in frontal] or [0.0]
    similarity = float(np.mean(sims_front))
    checks["coincide_identidad"] = similarity >= match_threshold

    sims_all = [cosine(o.embedding, template) for i in valid for o in valid[i]]
    checks["misma_persona"] = min(sims_all) >= CONSISTENCY_THRESHOLD

    prints = [o.fingerprint for i in valid for o in valid[i]]
    by_step = [{o.fingerprint for o in valid[i]} for i in range(len(challenge))]
    repeated = any(by_step[a] & by_step[b] for a in range(len(by_step)) for b in range(a + 1, len(by_step)))
    checks["fotogramas_distintos"] = not repeated and len(set(prints)) > 1

    order = ["un_rostro", "fotogramas_distintos", "nitidez", "poses", "rango_giro", "misma_persona", "coincide_identidad"]
    reasons = {
        "fotogramas_distintos": "fotogramas_repetidos", "nitidez": "imagen_borrosa", "poses": "pose_incorrecta",
        "rango_giro": "sin_movimiento_3d", "misma_persona": "identidad_inconsistente",
        "coincide_identidad": "identidad_no_coincide",
    }
    for name in order:
        if not checks.get(name, True):
            return LivenessResult(False, reasons.get(name, name), checks, similarity)
    return LivenessResult(True, "verificado", checks, similarity)
