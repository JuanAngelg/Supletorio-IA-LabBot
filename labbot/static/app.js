// Interfaz del asistente. Las imágenes del rostro van directo al servicio facial;
// el backend solo recibe el identificador de la verificación y valida la evidencia firmada.
const $ = (s) => document.querySelector(s);
const state = { sid: null, token: null, user: null, cfg: null, retryText: null };
const store = {
  all() { try { return JSON.parse(localStorage.getItem("labbot.sessions") || "{}"); } catch { return {}; } },
  put(sid, v) { try { const a = store.all(); a[sid] = v; localStorage.setItem("labbot.sessions", JSON.stringify(a)); } catch {} },
};

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (state.sid) { headers["X-Session-Id"] = state.sid; headers["X-Session-Token"] = state.token; }
  const res = await fetch(path, { ...opts, headers });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(detail(body) || res.statusText), { status: res.status, body });
  return body;
}
const detail = (b) => typeof b.detail === "string" ? b.detail : (b.detail && b.detail.message) || "";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// --- arranque -------------------------------------------------------------
async function init() {
  state.cfg = await api("/api/config");
  $("#llmInfo").textContent = `LLM: ${state.cfg.llm.proveedor} · ${state.cfg.llm.modelo}`;
  const users = await api("/api/users");
  $("#userSelect").innerHTML = users.map((u) => `<option value="${u.id}">${esc(u.name)} — ${u.role} (${u.id})</option>`).join("");
  const last = localStorage.getItem("labbot.current");
  const saved = store.all();
  if (last && saved[last]) await openSession(last, saved[last].token);
}

async function newSession() {
  state.sid = null;
  const r = await api("/api/sessions", { method: "POST", body: JSON.stringify({ user_id: $("#userSelect").value }) });
  store.put(r.session_id, { token: r.token, user_id: r.user_id, name: r.user_name });
  await openSession(r.session_id, r.token);
}

async function openSession(sid, token) {
  state.sid = sid; state.token = token;
  try { localStorage.setItem("labbot.current", sid); } catch {}
  let info;
  try { info = await api("/api/session"); } catch { state.sid = null; return; }
  state.user = info;
  $("#whoami").textContent = `${info.user_name} · ${info.role} · ${sid}`;
  $("#userSelect").value = info.user_id;
  $("#chatInput").disabled = false; $("#sendBtn").disabled = false;
  renderAuth(info.autenticacion);
  const box = $("#messages"); box.innerHTML = "";
  if (!info.mensajes.length) box.innerHTML = `<div class="empty">Sesión nueva. Pruebe: «¿Qué requisitos debo cumplir para solicitar un osciloscopio?»</div>`;
  for (const m of info.mensajes) addMessage(m.role, m.content, m.role === "assistant" ? { request_id: m.request_id, ...m.meta } : null, true);
  await loadSessions();
}

async function loadSessions() {
  const list = await api("/api/sessions");
  const saved = store.all();
  $("#sessionList").innerHTML = list.map((s) => `<li data-sid="${s.id}" class="${s.id === state.sid ? "active" : ""}">
    ${esc(s.title || s.id)}<br><span class="muted small">${s.last_activity.replace("T", " ")} · ${s.mensajes} mensajes${saved[s.id] ? "" : " · sin token local"}</span></li>`).join("");
}

function renderAuth(a) {
  const b = $("#authBadge");
  b.className = "badge " + (a.autenticado ? "on" : "off");
  b.textContent = a.autenticado ? `Verificado (${a.metodo}) · ${Math.round(a.vence_en_s / 60)} min` : "Sin verificar";
}

// --- mensajes ---------------------------------------------------------------
function addMessage(role, text, meta, historic = false) {
  const box = $("#messages");
  box.querySelector(".empty")?.remove();
  const el = document.createElement("div");
  el.className = "msg " + role;
  el.textContent = text;
  if (meta && meta.request_id) {
    const m = document.createElement("div");
    m.className = "meta";
    m.innerHTML = `<span class="chip link" data-rid="${meta.request_id}">${meta.request_id}</span>` +
      (meta.estado ? `<span class="chip st-${meta.estado}">${meta.estado}</span>` : "") +
      (meta.ruta ? `<span class="chip">${esc(meta.ruta)}</span>` : "") +
      (meta.herramientas || []).map((t) => `<span class="chip">${esc(t.name)}:${esc(t.status)}</span>`).join("") +
      (meta.metricas ? `<span>${meta.metricas.latencia_ms} ms · ${meta.metricas.tokens_entrada}+${meta.metricas.tokens_salida} tok</span>` : "");
    el.appendChild(m);
    if (meta.pendiente && !historic) el.appendChild(pendingCard(meta.pendiente));
  }
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function pendingCard(p) {
  const c = document.createElement("div");
  c.className = "pending";
  c.innerHTML = `<div class="small"><b>Pendiente de confirmación</b> (${p.id})</div><div>${esc(p.resumen)}</div>
    <div class="row end"><button class="ghost" data-d="rechazar">Cancelar</button><button class="primary" data-d="confirmar">Confirmar</button></div>`;
  c.querySelectorAll("button").forEach((b) => b.onclick = async () => {
    c.querySelectorAll("button").forEach((x) => x.disabled = true);
    try { const r = await api("/api/confirm", { method: "POST", body: JSON.stringify({ pending_id: p.id, decision: b.dataset.d }) }); showReply(r); }
    catch (e) { addMessage("assistant", "Error: " + e.message); }
  });
  return c;
}

function showReply(r) {
  addMessage("assistant", r.respuesta, r);
  renderAuth(r.autenticacion);
  for (const a of r.acciones_ui || []) {
    if (a.tipo === "captura_facial") verifyFace(a);
    if (a.tipo === "codigo_alternativo") openTotp();
  }
  loadTrace(r.request_id);
  if (!$("#tab-db").classList.contains("hidden")) loadDb();
}

async function send(text) {
  addMessage("user", text);
  $("#chatInput").value = "";
  const strategy = document.querySelector("input[name=strategy]:checked").value;
  try {
    const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text, strategy }) });
    if (r.estado === "requiere_autenticacion") state.retryText = text;
    showReply(r);
    loadSessions();
  } catch (e) { addMessage("assistant", "Error: " + e.message); }
}

// --- cámara -------------------------------------------------------------------
const STEP_TEXT = { frente: "Mire al frente", izquierda: "Gire la cabeza hacia SU izquierda ←", derecha: "Gire la cabeza hacia SU derecha →" };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let stream = null;

async function startCamera() {
  stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: "user" }, audio: false });
  $("#video").srcObject = stream;
  await sleep(700);
}
function stopCamera() { stream?.getTracks().forEach((t) => t.stop()); stream = null; }
function grab() {
  const v = $("#video"), c = $("#canvas");
  c.width = 640; c.height = Math.round(640 * v.videoHeight / v.videoWidth) || 480;
  c.getContext("2d").drawImage(v, 0, 0, c.width, c.height); // sin espejar: el servicio espera la imagen real
  return c.toDataURL("image/jpeg", 0.85).split(",")[1];
}
function waitClick(btn) { return new Promise((res) => { btn.onclick = () => res(true); $("#faceCancel").onclick = () => res(false); }); }

async function verifyFace(action) {
  const dlg = $("#faceDialog");
  $("#faceTitle").textContent = "Verificación facial 1:1";
  $("#consentBox").classList.add("hidden");
  $("#faceStep").textContent = `Siga ${action.desafio.length} indicaciones. Pulse Comenzar.`;
  dlg.showModal();
  try { await startCamera(); } catch { $("#faceStep").textContent = "No se pudo abrir la cámara. Use el código alternativo."; }
  const go = await waitClick($("#faceGo"));
  if (!go || !stream) { stopCamera(); dlg.close(); return; }
  $("#faceGo").disabled = true;
  const frames = [];
  for (let i = 0; i < action.desafio.length; i++) {
    $("#faceStep").textContent = STEP_TEXT[action.desafio[i]];
    await sleep(1600);
    for (let k = 0; k < 3; k++) { frames.push({ paso: i, imagen: grab() }); await sleep(250); }
  }
  $("#faceStep").textContent = "Verificando…";
  stopCamera();
  try {
    const res = await fetch(`${action.face_url}/v1/verifications/${action.verification_id}/frames`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ frames }) });
    const out = await res.json();
    const done = await api("/api/auth/face/complete", { method: "POST", body: JSON.stringify({ verification_id: action.verification_id }) });
    if (done.autenticado) {
      $("#faceStep").textContent = "Identidad verificada por el sistema.";
      renderAuth(await api("/api/auth/state"));
      await sleep(900); dlg.close();
      if (state.retryText) { const t = state.retryText; state.retryText = null; send(t); }
    } else {
      $("#faceStep").textContent = `No verificado (${out.motivo || done.motivo}). Intentos restantes: ${out.intentos_restantes ?? "?"}.`;
      addMessage("assistant", `La verificación facial no fue aprobada (${out.motivo || done.motivo}). Puede reintentar o usar el código alternativo.`);
      await sleep(1800); dlg.close();
    }
  } catch (e) { $("#faceStep").textContent = "Servicio facial no disponible: " + e.message; await sleep(1800); dlg.close(); }
  finally { $("#faceGo").disabled = false; }
}

async function startFace() {
  try { verifyFace(await api("/api/auth/face/start", { method: "POST" })); }
  catch (e) { addMessage("assistant", "No se pudo iniciar la verificación facial: " + e.message); if (e.status === 409) openTotp(); }
}

async function enroll() {
  let t;
  try { t = await api("/api/biometrics/ticket", { method: "POST", body: JSON.stringify({ purpose: "enrolar" }) }); }
  catch (e) { addMessage("assistant", "Para enrolar su rostro primero verifique su identidad con el código alternativo. " + e.message); return; }
  const dlg = $("#faceDialog");
  $("#faceTitle").textContent = "Enrolamiento facial (voluntario)";
  $("#consentBox").classList.remove("hidden"); $("#consentCheck").checked = false;
  $("#faceStep").textContent = "Lea el consentimiento. Mire al frente con buena luz.";
  dlg.showModal();
  try { await startCamera(); } catch { $("#faceStep").textContent = "No se pudo abrir la cámara."; }
  const go = await waitClick($("#faceGo"));
  if (!go || !stream) { stopCamera(); dlg.close(); return; }
  if (!$("#consentCheck").checked) { $("#faceStep").textContent = "Sin consentimiento no se enrola."; stopCamera(); await sleep(1200); dlg.close(); return; }
  const frames = [];
  for (let k = 0; k < 5; k++) { $("#faceStep").textContent = `Capturando ${k + 1}/5…`; frames.push(grab()); await sleep(400); }
  stopCamera();
  const res = await fetch(`${t.face_url}/v1/enrollments`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticket: t.ticket, consentimiento: { aceptado: true }, frames }) });
  const out = await res.json();
  $("#faceStep").textContent = res.ok ? "Rostro enrolado. Ya puede usar la verificación facial." : "No se pudo enrolar: " + (out.detail || res.status);
  await sleep(1800); dlg.close();
}

async function deleteBio() {
  if (!confirm("¿Eliminar definitivamente su plantilla facial?")) return;
  try {
    const t = await api("/api/biometrics/ticket", { method: "POST", body: JSON.stringify({ purpose: "eliminar" }) });
    const res = await fetch(`${t.face_url}/v1/enrollments/${t.user_id}`, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticket: t.ticket }) });
    const out = await res.json();
    addMessage("assistant", out.eliminado ? "Su plantilla biométrica fue eliminada." : "No había una plantilla registrada.");
  } catch (e) { addMessage("assistant", "Verifique su identidad antes de eliminar datos biométricos. " + e.message); }
}

function openTotp() { $("#totpMsg").textContent = ""; $("#totpInput").value = ""; $("#totpDialog").showModal(); }
$("#totpCancel").onclick = () => $("#totpDialog").close();
$("#totpForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const r = await api("/api/auth/fallback", { method: "POST", body: JSON.stringify({ code: $("#totpInput").value.trim() }) });
    renderAuth(r); $("#totpDialog").close();
    if (state.retryText) { const t = state.retryText; state.retryText = null; send(t); }
  } catch (e) { $("#totpMsg").textContent = e.message; }
});

// --- trazabilidad -----------------------------------------------------------
async function loadTrace(rid) {
  try {
    const r = await api(`/api/audit/request/${rid}`);
    const c = r.completitud;
    $("#auditView").innerHTML = `<div><b>${rid}</b> · ${c.completo ? "traza completa" : "traza INCOMPLETA: " + c.faltantes.join(", ")} · ${c.eventos} eventos</div>` +
      r.eventos.map((e) => `<div class="ev ${e.event_type}"><b>#${e.seq} ${e.event_type}</b> <span class="muted">${e.ts.slice(11, 23)}</span>
        <pre>${esc(JSON.stringify(e.data, null, 1))}</pre><div class="muted">hash ${e.hash.slice(0, 16)}… ← ${e.prev_hash.slice(0, 10)}…</div></div>`).join("");
  } catch (e) { $("#auditView").textContent = "No se pudo cargar la traza: " + e.message; }
}

async function exportAudit() {
  const res = await fetch(`/api/audit/session/${state.sid}/export`, { headers: { "X-Session-Id": state.sid, "X-Session-Token": state.token } });
  if (!res.ok) { alert("No autorizado"); return; }
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = `auditoria_${state.sid}.json`; a.click();
}

async function verifyIntegrity() {
  try {
    const r = await api("/api/audit/verify");
    $("#integrity").innerHTML = `<b>${r.integro ? "ÍNTEGRO" : "ALTERADO"}</b> · ${r.eventos} eventos · anclajes ${r.anclajes_verificados}/${r.anclajes_total}` +
      (r.problemas.length ? `<pre>${esc(JSON.stringify(r.problemas.slice(0, 8), null, 1))}</pre>` : "");
  } catch (e) { $("#integrity").textContent = e.message; }
}

async function loadDb() {
  try {
    const r = await api("/api/demo/state");
    const b = r.bd;
    if (b.error) { $("#dbView").textContent = b.error; return; }
    const tbl = (rows, cols) => `<table><tr>${cols.map((c) => `<th>${c}</th>`).join("")}</tr>${rows.map((x) => `<tr>${cols.map((c) => `<td>${esc(x[c])}</td>`).join("")}</tr>`).join("")}</table>`;
    $("#dbView").innerHTML = `<h2>Reservas</h2>${tbl(b.reservas, ["codigo", "equipment_id", "user_id", "start_ts", "end_ts", "status"])}
      <h2>Préstamos</h2>${tbl(b.prestamos, ["codigo", "equipment_id", "user_id", "due_ts", "status"])}
      <h2>Equipos</h2>${tbl(b.equipos, ["id", "type", "status"])}`;
    document.querySelectorAll("[data-fault]").forEach((cb) => cb.checked = !!r.fallos[cb.dataset.fault]);
  } catch (e) { $("#dbView").textContent = e.message; }
}

// --- eventos ------------------------------------------------------------------
$("#newSessionBtn").onclick = newSession;
$("#chatForm").onsubmit = (ev) => { ev.preventDefault(); const t = $("#chatInput").value.trim(); if (t) send(t); };
$("#faceBtn").onclick = startFace;
$("#totpBtn").onclick = openTotp;
$("#enrollBtn").onclick = enroll;
$("#deleteBioBtn").onclick = deleteBio;
$("#exportBtn").onclick = exportAudit;
$("#verifyBtn").onclick = verifyIntegrity;
$("#refreshDb").onclick = loadDb;
$("#resetDemo").onclick = async () => { await api("/api/demo/reset", { method: "POST" }); loadDb(); };
$("#messages").addEventListener("click", (ev) => { const rid = ev.target.dataset?.rid; if (rid) loadTrace(rid); });
$("#sessionList").addEventListener("click", (ev) => {
  const li = ev.target.closest("li"); if (!li) return;
  const saved = store.all()[li.dataset.sid];
  if (saved) openSession(li.dataset.sid, saved.token);
});
document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab !== "audit") loadDb();
});
document.querySelectorAll("[data-fault]").forEach((cb) => cb.onchange = async () => {
  const value = cb.checked ? (cb.dataset.list ? cb.dataset.list.split(",") : true) : false;
  await api("/api/demo/faults", { method: "POST", body: JSON.stringify({ name: cb.dataset.fault, value }) });
});
init();
