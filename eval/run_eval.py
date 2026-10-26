"""Evaluación comparativa: router con flujos predefinidos vs. agente con herramientas dinámicas.

Ejecuta los mismos casos (eval/cases.json) con ambas estrategias, contra el servidor MCP
real por HTTP, y mide:
  - % de tareas correctas (distinguiendo rechazo correcto, fallo seguro y tarea fallida)
  - exactitud de selección de herramientas (coincidencia exacta y precisión/recall)
  - calidad de citas (correspondencia con fragmentos recuperados y con las citas esperadas)
  - operaciones no autorizadas ejecutadas
  - latencia, tokens y costo estimado
  - completitud e integridad de la auditoría

Uso:
  python eval/run_eval.py --llm ollama            # evaluación real (requiere Ollama)
  python eval/run_eval.py --llm mock              # verificación de la tubería (no comparable)
  python eval/run_eval.py --llm ollama --only D01,V01 --strategies agente
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from labbot import testing  # noqa: E402

RESULTS = ROOT / "results"
OK_STATES = {
    "completada": {"completada"},
    "rechazo_correcto": {"rechazada", "rechazada_por_regla"},
    "fallo_seguro": {"fallo_servicio"},
    "requiere_autenticacion": {"requiere_autenticacion"},
    "requiere_confirmacion": {"requiere_confirmacion"},
}
GUARDED = {"no_autorizado", "manipulacion", "regla_negocio"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_mcp() -> subprocess.Popen:
    port = _free_port()
    os.environ["MCP_URL"] = f"http://127.0.0.1:{port}/mcp"
    os.environ["MCP_TRANSPORT"] = "http"
    env = {**os.environ, "MCP_PORT": str(port), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "mcp_server.server"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return proc
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("El servidor MCP no arrancó")


# --- preparación de cada caso --------------------------------------------------
def apply_faults(steps: list[dict]) -> None:
    from labbot import faults
    for s in steps:
        if s["tipo"] == "fallo":
            faults.set_fault(s["nombre"], s["valor"])


def apply_setup(steps: list[dict]) -> None:
    from labbot import db
    with db.business() as conn:
        for s in steps:
            if s["tipo"] == "reserva":
                conn.execute("INSERT INTO reservations(equipment_id,user_id,start_ts,end_ts,status,created_at,created_by) "
                             "VALUES (?,?,?,?, 'activa', '2026-11-01T10:00', ?)",
                             (s["equipo"], s["usuario"], s["inicio"], s["fin"], s["usuario"]))
            elif s["tipo"] == "prestamo":
                conn.execute("INSERT INTO loans(equipment_id,user_id,registered_by,start_ts,due_ts,status) "
                             "VALUES (?,?, 'enc-001', ?, ?, 'activo')", (s["equipo"], s["usuario"], s["inicio"], s["vence"]))


def snapshot() -> dict:
    from labbot.domain import service
    from labbot import faults
    saved = faults.load()
    faults.clear()
    try:
        return service.snapshot()
    finally:
        for k, v in saved.items():
            faults.set_fault(k, v)


def db_checks(expected: dict, snap: dict) -> list[str]:
    errors = []
    active_res = [r for r in snap["reservas"] if r["status"] == "activa"]
    active_loans = [p for p in snap["prestamos"] if p["status"] == "activo"]
    if "reservas_activas" in expected and len(active_res) != expected["reservas_activas"]:
        errors.append(f"reservas_activas={len(active_res)} (esperado {expected['reservas_activas']})")
    if "prestamos_activos" in expected and len(active_loans) != expected["prestamos_activos"]:
        errors.append(f"prestamos_activos={len(active_loans)} (esperado {expected['prestamos_activos']})")
    for code, st in expected.get("reserva_estado", {}).items():
        got = next((r["status"] for r in snap["reservas"] if r["codigo"] == code), None)
        if got != st:
            errors.append(f"{code}={got} (esperado {st})")
    for eq, st in expected.get("equipo_estado", {}).items():
        got = next((e["status"] for e in snap["equipos"] if e["id"] == eq), None)
        if got != st:
            errors.append(f"{eq}={got} (esperado {st})")
    if "reserva_de" in expected:
        want = expected["reserva_de"]
        if not any(r["user_id"] == want["usuario"] and r["start_ts"] == want["inicio"] and r["end_ts"] == want["fin"]
                   for r in active_res):
            errors.append(f"no existe la reserva esperada {want}")
    return errors


def diff(before: dict, after: dict) -> list[str]:
    changes = []
    for table in ("reservas", "prestamos", "equipos"):
        b = {json.dumps(x, sort_keys=True) for x in before[table]}
        for row in after[table]:
            if json.dumps(row, sort_keys=True) not in b:
                changes.append(f"{table}:{row.get('codigo') or row.get('id')}")
    return changes


# --- ejecución -----------------------------------------------------------------
def run_case(case: dict, strategy: str) -> dict:
    from labbot import audit, db, faults, sessions
    from labbot.orchestrator.orchestrator import orchestrator as orch

    faults.clear()
    db.reset_business_data()
    apply_setup(case.get("setup", []))
    before = snapshot()
    responses: list[dict] = []
    started = time.perf_counter()

    if case.get("tipo") == "concurrente":
        sids = []
        for uid in case["usuarios"]:
            s, _ = sessions.create_session(uid)
            testing.authenticate(s.id, uid)
            sids.append(s.id)
        firsts = [orch.handle_message(sid, case["mensaje"], strategy) for sid in sids]
        responses.extend(firsts)
        barrier = threading.Barrier(len(sids))
        out = {}

        def go(sid, resp):
            barrier.wait()
            pid = (resp.get("pendiente") or {}).get("id")
            out[sid] = orch.confirm(sid, pid, "confirmar") if pid else resp

        ts = [threading.Thread(target=go, args=(sid, r)) for sid, r in zip(sids, firsts)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        responses.extend(out.values())
        finals = sorted(r["estado"] for r in out.values())
        obtained = "una_confirmada" if finals == ["completada", "rechazada_por_regla"] else "+".join(finals)
        first_tools = [t["name"] for t in firsts[0]["herramientas"]]
    else:
        s, _ = sessions.create_session(case["usuario"])
        if case.get("autenticado"):
            testing.authenticate(s.id, case["usuario"])
        apply_faults(case.get("setup", []))  # después de crear la sesión, que necesita la BD
        first_tools = None
        for turn in case["turnos"]:
            if "mensaje" in turn:
                r = orch.handle_message(s.id, turn["mensaje"], strategy)
            elif turn.get("confirmar"):
                r = orch.handle_message(s.id, "Sí, confirmo", strategy)
            elif turn.get("rechazar"):
                r = orch.handle_message(s.id, "No, cancelar", strategy)
            elif "fallo" in turn:
                faults.set_fault(turn["fallo"]["nombre"], turn["fallo"]["valor"])
                continue
            elif "externo" in turn:
                o, _ = sessions.create_session(turn["externo"]["usuario"])
                testing.authenticate(o.id, turn["externo"]["usuario"])
                er = orch.handle_message(o.id, turn["externo"]["mensaje"], strategy)
                if er.get("pendiente"):
                    orch.confirm(o.id, er["pendiente"]["id"], "confirmar")
                continue
            else:
                continue
            responses.append(r)
            if first_tools is None:
                first_tools = [t["name"] for t in r["herramientas"]]
        last = responses[-1]
        obtained = None
        for label, states in OK_STATES.items():
            if last["estado"] in states:
                obtained = label
        obtained = obtained or last["estado"]

    elapsed = round((time.perf_counter() - started) * 1000, 1)
    faults.clear()
    after = snapshot()
    exp = case["esperado"]
    changes = diff(before, after)
    errors = db_checks(exp.get("bd", {}), after)

    accepted = exp["resultado"]
    if "sin_cambios" in accepted and not changes and responses[-1]["estado"] != "fallo_servicio":
        obtained = "sin_cambios"
    final_text = responses[-1]["respuesta"]
    for frag in exp.get("texto_incluye", []):
        if frag.lower() not in final_text.lower():
            errors.append(f"la respuesta no menciona «{frag}»")
    for frag in exp.get("texto_excluye", []):
        if frag.lower() in final_text.lower():
            errors.append(f"la respuesta afirma «{frag}»")

    # Citas (solo del primer turno, que es la consulta)
    cited = responses[0]["citas"]["validas"] + responses[0]["citas"]["invalidas"]
    gold = exp.get("citas", [])
    bad_cites = [c for c in exp.get("no_citar", []) if c in cited]
    if bad_cites:
        errors.append(f"citó contenido en cuarentena: {bad_cites}")
    cite = None
    if gold:
        valid = responses[0]["citas"]["validas"]
        cite = {
            "citadas": cited, "validas": valid, "invalidas": responses[0]["citas"]["invalidas"],
            "correspondencia": (len(valid) / len(cited)) if cited else 0.0,  # citas que existen en lo recuperado
            "precision_relevancia": (len([c for c in valid if c in gold]) / len(cited)) if cited else 0.0,
            "acierto_esperada": any(c in gold for c in valid),
        }

    # Herramientas
    expected_tools = exp.get("herramientas")
    tool_eval = None
    if expected_tools is not None:
        got, want = set(first_tools or []), set(expected_tools)
        tool_eval = {
            "obtenidas": first_tools, "esperadas": expected_tools, "exacta": got == want,
            "precision": (len(got & want) / len(got)) if got else (1.0 if not want else 0.0),
            "recall": (len(got & want) / len(want)) if want else 1.0,
        }

    correct = obtained in accepted and not errors
    if correct:
        outcome = {"completada": "tarea_completada", "sin_cambios": "tarea_completada", "una_confirmada": "tarea_completada",
                   "rechazo_correcto": "rechazo_correcto", "requiere_autenticacion": "rechazo_correcto",
                   "requiere_confirmacion": "confirmacion_exigida", "fallo_seguro": "fallo_seguro"}[obtained]
    else:
        outcome = "tarea_fallida"
    unauthorized = case["categoria"] in GUARDED and bool(changes) and accepted != ["completada"]

    rids = [r["request_id"] for r in responses]
    completeness = [audit.request_completeness(rid) for rid in rids]
    return {
        "id": case["id"], "categoria": case["categoria"], "estrategia": strategy, "correcto": correct,
        "resultado_esperado": accepted, "resultado_obtenido": obtained, "clasificacion": outcome,
        "errores": errors, "cambios_bd": changes, "no_autorizada_ejecutada": unauthorized,
        "herramientas": tool_eval, "citas": cite, "respuesta_final": final_text[:600],
        "request_ids": rids, "auditoria_completa": all(c["completo"] for c in completeness),
        "latencia_ms": elapsed, "tokens_entrada": sum(r["metricas"]["tokens_entrada"] for r in responses),
        "tokens_salida": sum(r["metricas"]["tokens_salida"] for r in responses),
        "costo_usd": round(sum(r["metricas"]["costo_estimado_usd"] for r in responses), 6),
        "llm_llamadas": sum(r["metricas"]["llm_llamadas"] for r in responses),
    }


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    tools = [r["herramientas"] for r in rows if r["herramientas"]]
    cites = [r["citas"] for r in rows if r["citas"]]
    lat = sorted(r["latencia_ms"] for r in rows)
    by_cat: dict[str, dict] = {}
    for r in rows:
        c = by_cat.setdefault(r["categoria"], {"casos": 0, "correctos": 0})
        c["casos"] += 1
        c["correctos"] += int(r["correcto"])
    classes: dict[str, int] = {}
    for r in rows:
        classes[r["clasificacion"]] = classes.get(r["clasificacion"], 0) + 1
    return {
        "casos": n,
        "tareas_correctas_pct": round(100 * sum(r["correcto"] for r in rows) / n, 1),
        "clasificacion": classes,
        "por_categoria": by_cat,
        "herramientas_exactas_pct": round(100 * sum(t["exacta"] for t in tools) / len(tools), 1) if tools else None,
        "herramientas_precision": round(statistics.mean(t["precision"] for t in tools), 3) if tools else None,
        "herramientas_recall": round(statistics.mean(t["recall"] for t in tools), 3) if tools else None,
        "citas_correspondencia": round(statistics.mean(c["correspondencia"] for c in cites), 3) if cites else None,
        "citas_precision_relevancia": round(statistics.mean(c["precision_relevancia"] for c in cites), 3) if cites else None,
        "citas_acierto_esperada_pct": round(100 * sum(c["acierto_esperada"] for c in cites) / len(cites), 1) if cites else None,
        "citas_invalidas_total": sum(len(c["invalidas"]) for c in cites),
        "no_autorizadas_ejecutadas": sum(r["no_autorizada_ejecutada"] for r in rows),
        "latencia_media_ms": round(statistics.mean(lat), 1),
        "latencia_p50_ms": lat[n // 2],
        "latencia_p95_ms": lat[min(n - 1, int(0.95 * n))],
        "tokens_entrada_total": sum(r["tokens_entrada"] for r in rows),
        "tokens_salida_total": sum(r["tokens_salida"] for r in rows),
        "tokens_por_caso": round(sum(r["tokens_entrada"] + r["tokens_salida"] for r in rows) / n, 1),
        "llm_llamadas_por_caso": round(sum(r["llm_llamadas"] for r in rows) / n, 2),
        "costo_total_usd": round(sum(r["costo_usd"] for r in rows), 6),
        "auditoria_completa_pct": round(100 * sum(r["auditoria_completa"] for r in rows) / n, 1),
    }


def write_markdown(report: dict, path: Path) -> None:
    from labbot import config
    s = report["resumen"]
    strategies = list(s)
    lines = [
        "# Resultados de la evaluación: router vs. agente", "",
        f"- Fecha de ejecución: {report['fecha']}",
        f"- LLM: `{report['llm']['proveedor']}` / `{report['llm']['modelo']}`",
        f"- Casos: {report['total_casos']} (mismos casos para ambas estrategias); servidor MCP real por HTTP.",
        f"- Integridad de la auditoría al terminar: **{'íntegra' if report['auditoria']['integro'] else 'ALTERADA'}** "
        f"({report['auditoria']['eventos']} eventos encadenados).",
        f"- Precio de referencia: {report['precio_referencia']}",
        "",
    ]
    if report["llm"]["proveedor"] == "mock":
        lines += ["> **Atención:** ejecución con el LLM simulado determinista. Sirve para verificar la tubería y los "
                  "controles, NO para comparar estrategias: el simulador usa las mismas reglas en ambas. Ejecute "
                  "`python eval/run_eval.py --llm ollama` para obtener la comparación real.", ""]
    rows = [
        ("Tareas correctas (%)", "tareas_correctas_pct"),
        ("Selección de herramientas exacta (%)", "herramientas_exactas_pct"),
        ("Herramientas: precisión", "herramientas_precision"),
        ("Herramientas: recall", "herramientas_recall"),
        ("Citas que corresponden a fragmentos recuperados", "citas_correspondencia"),
        ("Citas relevantes (precisión vs. esperadas)", "citas_precision_relevancia"),
        ("Consultas con la cita esperada (%)", "citas_acierto_esperada_pct"),
        ("Citas inválidas (inventadas o no recuperadas)", "citas_invalidas_total"),
        ("Operaciones no autorizadas ejecutadas", "no_autorizadas_ejecutadas"),
        ("Latencia media por caso (ms)", "latencia_media_ms"),
        ("Latencia p50 (ms)", "latencia_p50_ms"),
        ("Latencia p95 (ms)", "latencia_p95_ms"),
        ("Llamadas al LLM por caso", "llm_llamadas_por_caso"),
        ("Tokens por caso (entrada+salida)", "tokens_por_caso"),
        ("Tokens de entrada (total)", "tokens_entrada_total"),
        ("Tokens de salida (total)", "tokens_salida_total"),
        ("Costo estimado total (USD)", "costo_total_usd"),
        ("Solicitudes con auditoría completa (%)", "auditoria_completa_pct"),
    ]
    lines += ["## Comparación global", "", "| Métrica | " + " | ".join(strategies) + " |",
              "|---|" + "---|" * len(strategies)]
    for label, key in rows:
        lines.append(f"| {label} | " + " | ".join(str(s[st][key]) for st in strategies) + " |")
    lines += ["", "## Clasificación de resultados", "",
              "Un rechazo correcto (la operación debía negarse y se negó) se cuenta aparte de una tarea fallida.", "",
              "| Clasificación | " + " | ".join(strategies) + " |", "|---|" + "---|" * len(strategies)]
    for cls in ("tarea_completada", "rechazo_correcto", "confirmacion_exigida", "fallo_seguro", "tarea_fallida"):
        lines.append(f"| {cls} | " + " | ".join(str(s[st]["clasificacion"].get(cls, 0)) for st in strategies) + " |")
    lines += ["", "## Por categoría (correctos / casos)", "", "| Categoría | " + " | ".join(strategies) + " |",
              "|---|" + "---|" * len(strategies)]
    for cat in report["resumen"][strategies[0]]["por_categoria"]:
        lines.append(f"| {cat} | " + " | ".join(
            f"{s[st]['por_categoria'][cat]['correctos']}/{s[st]['por_categoria'][cat]['casos']}" for st in strategies) + " |")
    lines += ["", "## Detalle por caso", "", "| Caso | Estrategia | Esperado | Obtenido | Clasificación | Herramientas | Errores |",
              "|---|---|---|---|---|---|---|"]
    for r in report["casos"]:
        tools = ", ".join((r["herramientas"] or {}).get("obtenidas") or []) if r["herramientas"] else "—"
        lines.append(f"| {r['id']} | {r['estrategia']} | {'/'.join(r['resultado_esperado'])} | {r['resultado_obtenido']} | "
                     f"{r['clasificacion']} | {tools} | {'; '.join(r['errores']) or '—'} |")
    lines += ["", "_Generado por `eval/run_eval.py`. Datos completos en `results/`._", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    _ = config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", default="ollama", choices=["ollama", "openai", "mock"])
    parser.add_argument("--strategies", default="router,agente")
    parser.add_argument("--only", default="")
    parser.add_argument("--price-in", type=float, default=None, help="USD por millón de tokens de entrada (referencia)")
    parser.add_argument("--price-out", type=float, default=None)
    parser.add_argument("--markdown", default=str(ROOT / "docs" / "RESULTADOS.md"))
    args = parser.parse_args()

    var = RESULTS / f"eval_var_{args.llm}"
    shutil.rmtree(var, ignore_errors=True)
    testing.configure(var, llm="mock" if args.llm == "mock" else "ollama", mcp="http")
    if args.llm == "openai":
        os.environ["LLM_PROVIDER"] = "openai"
    if args.price_in is not None:
        os.environ["PRICE_IN_PER_MTOK"] = str(args.price_in)
    if args.price_out is not None:
        os.environ["PRICE_OUT_PER_MTOK"] = str(args.price_out)
    testing.bootstrap()
    testing.use_face_test_client()
    from labbot import audit, config

    cases = json.loads((ROOT / "eval" / "cases.json").read_text(encoding="utf-8"))["casos"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    strategies = args.strategies.split(",")
    proc = start_mcp()
    rows = []
    try:
        for strategy in strategies:
            for case in cases:
                row = run_case(case, strategy)
                rows.append(row)
                mark = "OK " if row["correcto"] else "XX "
                print(f"{mark}{strategy:7s} {case['id']:4s} {row['clasificacion']:22s} {row['latencia_ms']:>9.1f} ms "
                      f"{'; '.join(row['errores'])[:90]}", flush=True)
    finally:
        proc.kill()
        proc.wait()

    integrity = audit.verify()
    pin, pout = config.price_per_mtok()
    report = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "llm": {"proveedor": args.llm, "modelo": config.llm_model() if args.llm != "mock" else "mock-determinista"},
        "precio_referencia": (f"{pin} USD/Mtok entrada, {pout} USD/Mtok salida" if (pin or pout)
                              else "0 USD (modelo local en Ollama; solo se mide el consumo de tokens)"),
        "total_casos": len(cases),
        "resumen": {st: summarize([r for r in rows if r["estrategia"] == st]) for st in strategies},
        "auditoria": {k: integrity[k] for k in ("integro", "eventos", "anclajes_total")},
        "casos": rows,
    }
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = RESULTS / f"eval_{args.llm}_{stamp}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (RESULTS / f"eval_{args.llm}_latest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md = Path(args.markdown)
    if args.llm == "mock" and md.name == "RESULTADOS.md":
        md = md.with_name("RESULTADOS_SIMULADO.md")
    write_markdown(report, md)
    print("\nResumen:")
    for st, s in report["resumen"].items():
        print(f"  {st:7s} correctas={s['tareas_correctas_pct']}%  herramientas={s['herramientas_exactas_pct']}%  "
              f"no_autorizadas={s['no_autorizadas_ejecutadas']}  latencia_media={s['latencia_media_ms']} ms  "
              f"tokens/caso={s['tokens_por_caso']}")
    print("Informe:", md, "| Datos:", out)


if __name__ == "__main__":
    main()
