"""Motores de análisis facial.

OpenCVFaceEngine (producción del prototipo):
  - Detección: YuNet (face_detection_yunet_2023mar.onnx) -> caja + 5 puntos faciales.
  - Reconocimiento: SFace (face_recognition_sface_2021dec.onnx) -> vector de 128 valores.
  - Pose: giro horizontal (yaw) aproximado con la posición de la nariz respecto al punto
    medio de los ojos, normalizada por la distancia entre ojos. En una foto plana este
    cociente casi no cambia al inclinarla, que es la base de la prueba de presencia.

SimulatedFaceEngine (SOLO pruebas automatizadas): cada "imagen" es un JSON que describe
al sujeto y su pose. Permite probar el flujo, la presencia y los ataques sin fotografías
de personas. Se activa con FACE_ENGINE=simulated y nunca en una demostración real.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODELS_DIR = Path(os.environ.get("FACE_MODELS_DIR", Path(__file__).resolve().parent.parent / "models"))
DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"


@dataclass
class Observation:
    n_faces: int
    embedding: np.ndarray | None = None
    yaw: float = 0.0
    sharpness: float = 0.0
    face_ratio: float = 0.0
    fingerprint: bytes = b""  # miniatura para detectar fotogramas idénticos repetidos


class FaceEngineError(Exception):
    pass


class OpenCVFaceEngine:
    name = "opencv-yunet-sface"
    # Umbral recomendado por OpenCV para SFace con similitud coseno.
    match_threshold = 0.363

    def __init__(self, models_dir: Path = MODELS_DIR):
        import cv2

        det, rec = models_dir / DETECTOR_FILE, models_dir / RECOGNIZER_FILE
        if not det.exists() or not rec.exists():
            raise FaceEngineError(
                f"Faltan los modelos en {models_dir}. Ejecute: python scripts/download_face_models.py"
            )
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(str(det), "", (320, 320), 0.85, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(rec), "")

    def analyze(self, image_bytes: bytes) -> Observation:
        cv2 = self.cv2
        img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise FaceEngineError("Imagen ilegible")
        h, w = img.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(img)
        n = 0 if faces is None else len(faces)
        if n != 1:
            return Observation(n_faces=n)
        face = faces[0]
        aligned = self.recognizer.alignCrop(img, face)
        feat = self.recognizer.feature(aligned).flatten().astype(np.float32)
        feat /= np.linalg.norm(feat) + 1e-9
        rex, rey, lex, ley, nx, ny = face[4], face[5], face[6], face[7], face[8], face[9]
        eye_dist = max(float(np.hypot(lex - rex, ley - rey)), 1.0)
        yaw = float((nx - (rex + lex) / 2) / eye_dist)
        gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        thumb = cv2.resize(gray, (16, 16)).tobytes()
        return Observation(1, feat, yaw, sharpness, float(face[2] * face[3]) / float(w * h), thumb)


class SimulatedFaceEngine:
    """Motor de prueba: la imagen es JSON {"sujeto", "yaw", "rostros", "nitidez", "ruido"}."""

    name = "simulated-test-only"
    match_threshold = 0.363

    @staticmethod
    def _identity_vector(subject: str) -> np.ndarray:
        seed = int.from_bytes(hashlib.sha256(subject.encode()).digest()[:8], "big")
        v = np.random.default_rng(seed).standard_normal(128).astype(np.float32)
        return v / np.linalg.norm(v)

    def analyze(self, image_bytes: bytes) -> Observation:
        try:
            d = json.loads(image_bytes.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FaceEngineError("Imagen simulada ilegible") from exc
        n = int(d.get("rostros", 1))
        if n != 1:
            return Observation(n_faces=n)
        base = self._identity_vector(d["sujeto"])
        noise_seed = int(d.get("ruido", 0))
        noise = np.random.default_rng(noise_seed).standard_normal(128).astype(np.float32)
        noise /= np.linalg.norm(noise)
        yaw = float(d.get("yaw", 0.0))
        # El giro de la cabeza reduce algo la similitud, como en un sistema real.
        feat = base + float(d.get("variacion", 0.35)) * noise + 0.8 * abs(yaw) * noise[::-1]
        feat /= np.linalg.norm(feat)
        fingerprint = d.get("huella", f"{d['sujeto']}|{yaw}|{noise_seed}").encode()
        return Observation(1, feat, yaw, float(d.get("nitidez", 120.0)), 0.2, fingerprint)


def create_engine():
    kind = os.environ.get("FACE_ENGINE", "opencv")
    if kind == "simulated":
        return SimulatedFaceEngine()
    return OpenCVFaceEngine()


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
