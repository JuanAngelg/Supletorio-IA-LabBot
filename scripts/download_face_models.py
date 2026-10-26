"""Descarga los modelos ONNX públicos de OpenCV Zoo (licencia Apache 2.0 / MIT).

- face_detection_yunet_2023mar.onnx   (~230 KB)  detector YuNet
- face_recognition_sface_2021dec.onnx (~37 MB)   reconocimiento SFace

Uso:  python scripts/download_face_models.py
"""
import sys
import urllib.request
from pathlib import Path

MODELS = {
    "face_detection_yunet_2023mar.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx":
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}

target = Path(__file__).resolve().parent.parent / "models"
target.mkdir(exist_ok=True)
for name, url in MODELS.items():
    path = target / name
    if path.exists() and path.stat().st_size > 100_000:
        print("ya existe:", path)
        continue
    print("descargando", url)
    urllib.request.urlretrieve(url, path)
    size = path.stat().st_size
    if size < 100_000:  # un puntero de Git LFS en lugar del modelo
        path.unlink()
        sys.exit(f"Descarga inválida de {name} ({size} bytes). Descárguelo manualmente desde {url}")
    print(f"  {name}: {size / 1e6:.1f} MB")
print("Modelos listos en", target)
