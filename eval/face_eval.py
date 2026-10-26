"""Falsas aceptaciones (FAR) y falsos rechazos (FRR) de la verificación facial.

Dos modos:

1) dataset — conjunto de imágenes de VOLUNTARIOS con consentimiento (o datos de prueba
   autorizados), con la estructura  DIR/<persona>/*.jpg  (al menos 4 imágenes por persona).
   Las primeras --enroll imágenes forman la plantilla; las restantes son sondas.
   Se comparan sondas genuinas (misma persona) e impostoras (otras plantillas).

     python eval/face_eval.py dataset --dir datos_voluntarios

2) intentos — intentos reales hechos en la interfaz durante la demostración. El evaluador
   etiqueta cada verification_id en un CSV:  verification_id,tipo  con tipo en
   {genuino, impostor, foto, video}. Se cruza con var/face.db (resultado de cada intento).

     python eval/face_eval.py intentos --csv intentos_etiquetados.csv

Se reportan tasas con intervalo de confianza de Wilson al 95 % y el tamaño de la muestra:
con pocos participantes los intervalos son muy amplios y los resultados no generalizan.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"tasa": round(k / n, 4) if n else None, "eventos": k, "n": n, "ic95": [round(lo, 4), round(hi, 4)]}


def run_dataset(args) -> dict:
    from face_service.engine import cosine, create_engine
    engine = create_engine()
    people, skipped = {}, {}
    for person in sorted(p for p in Path(args.dir).iterdir() if p.is_dir()):
        images = sorted(f for f in person.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png"))
        embs = []
        for img in images:
            obs = engine.analyze(img.read_bytes())
            if obs.n_faces == 1:
                embs.append(obs.embedding)
        if len(embs) > args.enroll:
            tpl = np.mean(embs[: args.enroll], axis=0)
            people[person.name] = {"plantilla": tpl / np.linalg.norm(tpl), "sondas": embs[args.enroll:],
                                   "descartadas": len(images) - len(embs)}
        else:
            skipped[person.name] = f"{len(embs)} de {len(images)} imágenes con un rostro; se necesitan más de {args.enroll}"
    genuine = [cosine(p, d["plantilla"]) for d in people.values() for p in d["sondas"]]
    impostor = [cosine(p, other["plantilla"]) for name, d in people.items() for p in d["sondas"]
                for oname, other in people.items() if oname != name]
    th = args.threshold if args.threshold is not None else engine.match_threshold
    sweep = []
    for t in np.round(np.arange(0.20, 0.61, 0.025), 3):
        sweep.append({"umbral": float(t), "FAR": round(float(np.mean(np.array(impostor) >= t)), 4) if impostor else None,
                      "FRR": round(float(np.mean(np.array(genuine) < t)), 4) if genuine else None})
    return {
        "modo": "dataset", "motor": engine.name, "umbral": th, "personas": len(people), "personas_descartadas": skipped,
        "comparaciones_genuinas": len(genuine), "comparaciones_impostoras": len(impostor),
        "imagenes_sin_rostro_unico": sum(d["descartadas"] for d in people.values()),
        "FAR": rate(int(np.sum(np.array(impostor) >= th)), len(impostor)),
        "FRR": rate(int(np.sum(np.array(genuine) < th)), len(genuine)),
        "barrido_umbral": sweep,
        "limitaciones": ("Muestra pequeña; las comparaciones impostoras entre las mismas personas no son "
                         "independientes; condiciones de captura homogéneas; no mide la comprobación de presencia."),
    }


def run_attempts(args) -> dict:
    from face_service import store
    labels = {}
    with open(args.csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            labels[row["verification_id"].strip()] = row["tipo"].strip()
    conn = sqlite3.connect(args.face_db or store.db_path())
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: r for r in conn.execute("SELECT id, status, reason FROM verifications")}
    counts: dict[str, dict] = {}
    for vid, kind in labels.items():
        if vid not in rows:
            continue
        accepted = rows[vid]["status"] == "verificada"
        c = counts.setdefault(kind, {"n": 0, "aceptados": 0, "motivos": {}})
        c["n"] += 1
        c["aceptados"] += int(accepted)
        c["motivos"][rows[vid]["reason"]] = c["motivos"].get(rows[vid]["reason"], 0) + 1
    g = counts.get("genuino", {"n": 0, "aceptados": 0})
    out = {"modo": "intentos", "por_tipo": counts,
           "FRR_genuinos": rate(g["n"] - g["aceptados"], g["n"])}
    for kind in ("impostor", "foto", "video"):
        c = counts.get(kind, {"n": 0, "aceptados": 0})
        out[f"aceptacion_{kind}"] = rate(c["aceptados"], c["n"])
    out["limitaciones"] = "Pocos intentos y participantes; ataques realizados por el propio equipo, no por atacantes expertos."
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="modo", required=True)
    d = sub.add_parser("dataset")
    d.add_argument("--dir", required=True)
    d.add_argument("--enroll", type=int, default=3)
    d.add_argument("--threshold", type=float, default=None)
    i = sub.add_parser("intentos")
    i.add_argument("--csv", required=True)
    i.add_argument("--face-db", default=None)
    parser.add_argument("--out", default=str(ROOT / "results" / "face_eval.json"))
    args = parser.parse_args()
    report = run_dataset(args) if args.modo == "dataset" else run_attempts(args)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "barrido_umbral"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
