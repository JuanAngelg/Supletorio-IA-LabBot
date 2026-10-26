"""Demuestra qué alteraciones del registro de auditoría se detectan y cuáles no.

Trabaja sobre COPIAS de var/audit.db y del archivo de anclajes. Cada ataque simula a
alguien con acceso directo al archivo SQLite (que puede borrar los triggers).

Uso:  python scripts/tamper_demo.py [--out results/tamper_report.json]
"""
import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labbot import audit, config  # noqa: E402


def _copy() -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp())
    db_file, anchors = tmp / "audit.db", tmp / "anchors.jsonl"
    src = sqlite3.connect(config.audit_db_path())
    dst = sqlite3.connect(db_file)
    src.backup(dst)
    src.close()
    dst.close()
    shutil.copy(config.audit_anchors_path(), anchors)
    return db_file, anchors


def _raw(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file, isolation_level=None)
    conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
    conn.execute("DROP TRIGGER IF EXISTS audit_no_delete")
    return conn


def attack_app_update(db_file, anchors):
    conn = sqlite3.connect(db_file, isolation_level=None)
    try:
        conn.execute("UPDATE audit_log SET event_type = 'x' WHERE seq = 1")
        return "la actualización se ejecutó"
    except sqlite3.DatabaseError as exc:
        return f"bloqueada por trigger: {exc}"


def attack_modify(db_file, anchors):
    conn = _raw(db_file)
    seq = conn.execute("SELECT seq FROM audit_log WHERE event_type = 'final_result' LIMIT 1").fetchone()[0]
    conn.execute("UPDATE audit_log SET data_json = replace(data_json, '\"estado\":\"', '\"estado\":\"X') WHERE seq = ?", (seq,))
    return f"se cambió el campo estado del evento {seq}"


def attack_delete_middle(db_file, anchors):
    conn = _raw(db_file)
    seq = conn.execute("SELECT seq FROM audit_log WHERE event_type = 'authz_denied' LIMIT 1").fetchone()
    seq = seq[0] if seq else conn.execute("SELECT MAX(seq)/2 FROM audit_log").fetchone()[0]
    conn.execute("DELETE FROM audit_log WHERE seq = ?", (seq,))
    return f"se borró el evento {seq} (p. ej. un rechazo de autorización)"


def attack_reorder(db_file, anchors):
    conn = _raw(db_file)
    a, b = 3, 4
    ra = conn.execute("SELECT data_json, event_type FROM audit_log WHERE seq = ?", (a,)).fetchone()
    rb = conn.execute("SELECT data_json, event_type FROM audit_log WHERE seq = ?", (b,)).fetchone()
    conn.execute("UPDATE audit_log SET data_json = ?, event_type = ? WHERE seq = ?", (rb[0], rb[1], a))
    conn.execute("UPDATE audit_log SET data_json = ?, event_type = ? WHERE seq = ?", (ra[0], ra[1], b))
    return f"se intercambió el contenido de los eventos {a} y {b}"


def attack_insert_forged(db_file, anchors):
    conn = _raw(db_file)
    last = conn.execute("SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    conn.execute("INSERT INTO audit_log(seq, ts, request_id, session_id, user_id, event_type, data_json, prev_hash, hash) "
                 "VALUES (?, '2026-01-01T00:00:00', 'req_falso', NULL, 'est-001', 'db_change', '{}', ?, ?)",
                 (last[0] + 1, last[1], "f" * 64))
    return "se añadió al final un evento falso sin conocer la clave HMAC"


def attack_truncate_anchored(db_file, anchors):
    conn = _raw(db_file)
    last = conn.execute("SELECT MAX(seq) FROM audit_log").fetchone()[0]
    conn.execute("DELETE FROM audit_log WHERE seq > ?", (last - 5,))
    return "se borraron los 5 últimos eventos (ya anclados)"


def attack_truncate_unanchored(db_file, anchors):
    # El atacante borra también las líneas finales del archivo de anclajes -> límite.
    conn = _raw(db_file)
    lines = anchors.read_text(encoding="utf-8").splitlines()
    keep = lines[:-1]
    cut_after = json.loads(keep[-1])["seq"] if keep else 0
    conn.execute("DELETE FROM audit_log WHERE seq > ?", (cut_after,))
    anchors.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return f"se truncó el log tras seq {cut_after} y se borró el último anclaje"


def attack_rewrite_with_key(db_file, anchors):
    """Atacante interno con la clave HMAC: modifica un evento y recalcula toda la cadena."""
    conn = _raw(db_file)
    rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM audit_log LIMIT 1").description]
    prev = audit.GENESIS
    for row in rows:
        r = dict(zip(cols, row))
        if r["event_type"] == "authz_denied":
            r["data_json"] = '{"borrado":true}'
        record = {"seq": r["seq"], "ts": r["ts"], "request_id": r["request_id"], "session_id": r["session_id"],
                  "user_id": r["user_id"], "event_type": r["event_type"], "data": r["data_json"]}
        digest = audit.compute_hash(prev, record)
        conn.execute("UPDATE audit_log SET data_json = ?, prev_hash = ?, hash = ? WHERE seq = ?", (r["data_json"], prev, digest, r["seq"]))
        prev = digest
    return "con la clave HMAC se alteró un rechazo y se recalculó toda la cadena (anclajes intactos)"


def attack_rewrite_with_key_and_anchors(db_file, anchors):
    msg = attack_rewrite_with_key(db_file, anchors)
    conn = sqlite3.connect(db_file)
    rows = dict(conn.execute("SELECT seq, hash FROM audit_log").fetchall())
    new = []
    for line in anchors.read_text(encoding="utf-8").splitlines():
        a = json.loads(line)
        a["hash"] = rows.get(a["seq"], a["hash"])
        new.append(json.dumps(a))
    anchors.write_text("\n".join(new) + "\n", encoding="utf-8")
    return msg.replace("(anclajes intactos)", "y TAMBIÉN se reescribieron los anclajes")


ATTACKS = [
    ("UPDATE desde la aplicación", attack_app_update, "bloqueado"),
    ("Modificar un campo", attack_modify, "detecta"),
    ("Borrar un evento intermedio", attack_delete_middle, "detecta"),
    ("Reordenar eventos", attack_reorder, "detecta"),
    ("Insertar evento falso sin clave", attack_insert_forged, "detecta"),
    ("Truncar el final ya anclado", attack_truncate_anchored, "detecta"),
    ("Truncar y borrar el último anclaje", attack_truncate_unanchored, "limite"),
    ("Reescritura completa con la clave HMAC", attack_rewrite_with_key, "detecta"),
    ("Reescritura con clave + anclajes", attack_rewrite_with_key_and_anchors, "limite"),
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "results" / "tamper_report.json"))
    args = parser.parse_args()
    audit.anchor()
    base = audit.verify()
    print(f"Registro original: íntegro={base['integro']} eventos={base['eventos']} anclajes={base['anclajes_total']}\n")
    report = {"original": {k: base[k] for k in ("integro", "eventos", "anclajes_total")}, "ataques": []}
    for name, fn, expected in ATTACKS:
        db_file, anchors = _copy()
        action = fn(db_file, anchors)
        res = audit.verify(db_file, anchors)
        detected = not res["integro"]
        tipos = sorted({p["tipo"] for p in res["problemas"]})
        if expected == "bloqueado":
            outcome = "BLOQUEADO" if "bloqueada" in action else "NO BLOQUEADO"
        else:
            outcome = "DETECTADO" if detected else "NO DETECTADO"
        print(f"{name:42s} -> {outcome:13s} {', '.join(tipos)}\n   {action}")
        report["ataques"].append({"ataque": name, "accion": action, "resultado": outcome, "esperado": expected,
                                  "tipos_problema": tipos, "problemas": res["problemas"][:5]})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nInforme:", args.out)


if __name__ == "__main__":
    main()
