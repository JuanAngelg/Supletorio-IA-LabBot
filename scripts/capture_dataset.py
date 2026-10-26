"""Captura imágenes de un VOLUNTARIO con la cámara para eval/face_eval.py (modo dataset).

Solo con consentimiento expreso. Las imágenes quedan en datos_voluntarios/<alias>/ (excluido
de git) y deben borrarse al terminar la evaluación.

Se descartan los primeros cuadros (la cámara ajusta la exposición al encender) y solo se guardan
cuadros con luz suficiente y exactamente un rostro detectado.

Uso:  python scripts/capture_dataset.py --alias voluntario01 --n 8
"""
import argparse
import shutil
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from face_service.engine import create_engine  # noqa: E402

WARMUP_FRAMES = 30
MIN_BRIGHTNESS = 50
MAX_TRIES_PER_IMAGE = 40

parser = argparse.ArgumentParser()
parser.add_argument("--alias", required=True, help="seudónimo, nunca el nombre real")
parser.add_argument("--n", type=int, default=8)
parser.add_argument("--camera", type=int, default=0)
args = parser.parse_args()
if input("¿El voluntario otorgó consentimiento para esta evaluación? (si/no): ").strip().lower() not in ("si", "sí"):
    raise SystemExit("Sin consentimiento no se captura.")
out = ROOT / "datos_voluntarios" / args.alias
if out.exists():
    shutil.rmtree(out)  # una nueva captura reemplaza la anterior del mismo alias
out.mkdir(parents=True)
engine = create_engine()
cam = cv2.VideoCapture(args.camera)
if not cam.isOpened():
    raise SystemExit("No se pudo abrir la cámara (¿la usa otro programa?).")
for _ in range(WARMUP_FRAMES):
    cam.read()

saved = 0
for i in range(args.n):
    print(f"Captura {i + 1}/{args.n}: mire a la cámara (varíe levemente la expresión)")
    time.sleep(1.2)
    for _ in range(MAX_TRIES_PER_IMAGE):
        ok, frame = cam.read()
        if not ok:
            continue
        if cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean() < MIN_BRIGHTNESS:
            print("  imagen muy oscura, reintentando…")
            time.sleep(0.25)
            continue
        ok_enc, buf = cv2.imencode(".jpg", frame)
        n_faces = engine.analyze(buf.tobytes()).n_faces
        if n_faces != 1:
            print(f"  rostros detectados: {n_faces}; debe haber exactamente uno, reintentando…")
            time.sleep(0.25)
            continue
        (out / f"{i:02d}.jpg").write_bytes(buf.tobytes())
        saved += 1
        break
    else:
        print("  no se obtuvo una imagen válida para esta captura")
cam.release()
print(f"{saved}/{args.n} imágenes válidas en {out}")
if saved < 4:
    print("Se necesitan al menos 4: revise la luz y repita con el mismo alias.")
