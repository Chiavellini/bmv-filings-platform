const state = {
  bootstrap: null,
  bridgeAvailable: false,
  operations: new Map(),
  pendingConfirmation: null,
  poll: null,
  segmentSetup: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const LOCAL_API = document.querySelector('meta[name="analyst-console-api"]')?.content || "http://127.0.0.1:8765";
const METRIC_SECTIONS = {
  income: "Estado de resultados",
  balance: "Balance general",
  cashflow: "Flujo de efectivo",
  segment: "Segmentos",
  ratio: "Razones financieras",
  kpi: "Indicadores operativos",
};

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(new URL(path, LOCAL_API), {
      ...options,
      mode: "cors",
      headers: {
        "Content-Type": "application/json",
        "X-Analyst-Console": "1",
        ...(options.headers || {}),
      },
    });
  } catch (_error) {
    throw new Error("El puente local no está disponible en esta Mac.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || "No se pudo completar la acción.");
  return payload;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(element._timer);
  element._timer = setTimeout(() => { element.className = "toast"; }, 3600);
}

function status(element, text, tone = "ready") {
  element.className = `node-status ${tone}`;
  element.innerHTML = `<i></i>${escapeHtml(text)}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function modelCompanies() {
  const template = $("#segments-template");
  const selected = template.value;
  template.innerHTML = (state.bootstrap?.companies?.segments || []).map(row =>
      `<option value="${escapeHtml(row.slug)}">${escapeHtml(row.name)}</option>`
    ).join("") || '<option value="">No hay empresas disponibles</option>';
  if ([...template.options].some(option => option.value === selected)) template.value = selected;
}

function sectionLabel(section) {
  return METRIC_SECTIONS[section] || section || "Otras métricas";
}

function presetMetricKeys(payload) {
  const valid = new Set(payload.metrics.map(metric => metric.key));
  return new Set(payload.preset.sections.flatMap(section =>
    section.rows
      .filter(row => row.kind === "metric" && valid.has(row.key))
      .map(row => row.key)
  ));
}

function renderMetricList() {
  const search = $("#metric-search").value.trim().toLocaleLowerCase("es-MX");
  const selected = state.segmentSetup.selectedMetrics;
  const metrics = [...state.segmentSetup.metrics]
    .sort((a, b) => a.label.localeCompare(b.label, "es-MX"))
    .filter(metric => `${metric.label} ${sectionLabel(metric.section)}`.toLocaleLowerCase("es-MX").includes(search));
  $("#metric-list").innerHTML = metrics.map(metric => `
    <label class="metric-choice">
      <input type="checkbox" value="${escapeHtml(metric.key)}"${selected.has(metric.key) ? " checked" : ""}>
      <span><b>${escapeHtml(metric.label)}</b><small>${escapeHtml(sectionLabel(metric.section))}</small></span>
    </label>
  `).join("") || '<p class="empty-metrics">No hay métricas que coincidan con la búsqueda.</p>';
  const count = selected.size;
  $("#metric-selection-count").textContent = `${count} seleccionada${count === 1 ? "" : "s"}`;
  $("#model-summary").textContent = count
    ? `${count} métrica${count === 1 ? "" : "s"} · Hoja Segments`
    : "Selecciona al menos una métrica";
}

function renderSegmentSetup(payload) {
  state.segmentSetup = payload;
  state.segmentSetup.selectedMetrics = presetMetricKeys(payload);
  $("#metric-search").value = "";
  renderMetricList();
}

async function loadSegmentSetup(company = "") {
  const generate = $("#generate-model");
  generate.disabled = true;
  generate.querySelector("span").textContent = "Cargando métricas…";
  try {
    renderSegmentSetup(await api(`/api/segments/setup?company=${encodeURIComponent(company)}`));
  } catch (error) {
    toast(error.message, true);
  } finally {
    generate.disabled = false;
    generate.querySelector("span").textContent = "Generar hoja de segmentos";
  }
}

function collectSegmentRequest() {
  const selected = state.segmentSetup?.selectedMetrics || new Set();
  const grouped = new Map();
  state.segmentSetup.metrics.forEach(metric => {
    if (!selected.has(metric.key)) return;
    const section = sectionLabel(metric.section);
    if (!grouped.has(section)) grouped.set(section, []);
    grouped.get(section).push({ kind: "metric", label: metric.label, key: metric.key });
  });
  return {
    template_company: $("#segments-template").value,
    sections: [...grouped].map(([name, rows]) => ({ name, rows })),
  };
}

async function launchSegmentJob() {
  const request = collectSegmentRequest();
  if (!request.sections.length) {
    toast("Selecciona al menos una métrica financiera.", true);
    return null;
  }
  try {
    const job = await api("/api/segments/jobs", {
      method: "POST",
      body: JSON.stringify(request),
    });
    toast(`${job.label}: tarea iniciada.`);
    closeDialogs();
    await refresh();
    ensurePolling();
    return job;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function renderBootstrap(payload) {
  state.bridgeAvailable = true;
  state.bootstrap = payload;
  state.operations = new Map(payload.operations.map(item => [item.key, item]));

  const system = $("#system-state");
  $("#bridge-banner").hidden = true;
  setLocalControls(true);
  if (payload.estate.connected) {
    system.className = "system-state ready";
    system.innerHTML = `<span class="state-dot"></span><span>${payload.active_jobs ? `${payload.active_jobs} tarea${payload.active_jobs === 1 ? "" : "s"} en proceso` : "Espacio listo"}</span>`;
  } else {
    system.className = "system-state warning";
    system.innerHTML = `<span class="state-dot"></span><span>Estate desconectado</span>`;
  }

  status($("#alpha-status"), payload.alpha.running ? "Ejecutándose" : payload.alpha.installed ? "Listo" : "Requiere instalación", payload.alpha.running ? "running" : payload.alpha.installed ? "ready" : "warning");
  status($("#estate-status"), payload.estate.connected ? "Conectado" : "Desconectado", payload.estate.connected ? "ready" : "warning");
  status($("#soft-status"), payload.nodes.soft.core_available ? (payload.nodes.soft.updated || "Disponible") : "Falta generar", payload.nodes.soft.core_available ? "ready" : "warning");
  status($("#models-status"), payload.nodes.models.latest_available ? `Último ${payload.nodes.models.updated}` : "Listo", "ready");
  const estateToggle = $("#estate-toggle");
  estateToggle.dataset.mode = payload.estate.connected ? "disconnect" : "connect";
  estateToggle.textContent = payload.estate.connected ? "Desconectar y expulsar" : "Conectar Estate";
  estateToggle.classList.toggle("danger", payload.estate.connected);
  $("#estate-copy").textContent = payload.estate.connected
    ? `USB ${payload.estate.volume_name} conectado. Ya puedes usar Alpha, modelos y matrices.`
    : "Conecta el USB para usar la biblioteca de reportes y sus índices.";
  modelCompanies();
  renderJobs(payload.jobs);
}

function setLocalControls(enabled) {
  const selectors = [
    "#launch-alpha",
    "#estate-toggle",
    '[data-dialog="model-dialog"]',
    '[data-dialog="estate-dialog"]',
    "[data-operation]",
    "[data-confirm-operation]",
    "[data-open]",
  ];
  $$(selectors.join(",")).forEach(control => { control.disabled = !enabled; });
}

function renderBridgeUnavailable() {
  state.bridgeAvailable = false;
  state.bootstrap = null;
  state.operations = new Map();
  const system = $("#system-state");
  system.className = "system-state warning";
  system.innerHTML = '<span class="state-dot"></span><span>Servicios locales no disponibles</span>';
  $("#bridge-banner").hidden = false;
  status($("#alpha-status"), "Sin conexión local", "warning");
  status($("#estate-status"), "Sin conexión local", "warning");
  status($("#soft-status"), "Sin conexión local", "warning");
  status($("#models-status"), "Sin conexión local", "warning");
  $("#estate-copy").textContent = "Prepara esta Mac una sola vez para conectar el USB y ejecutar proyectos.";
  setLocalControls(false);
  renderJobs([]);
}

function renderJobs(jobs) {
  const list = $("#job-list");
  if (!jobs.length) {
    list.innerHTML = '<p class="empty-state">Todavía no se ha iniciado ninguna tarea.</p>';
    return;
  }
  list.innerHTML = jobs.map(job => `
    <div class="job-row" data-job="${escapeHtml(job.id)}">
      <div><b>${escapeHtml(job.label)}</b><small>${escapeHtml(nodeName(job.node))} · ${formatTime(job.created_at)}</small></div>
      <div><small>${escapeHtml(job.message)}</small></div>
      <span class="job-state ${escapeHtml(job.status)}"><i></i>${escapeHtml(jobStatus(job.status))}</span>
      <div class="job-actions">
        <button type="button" data-log="${escapeHtml(job.id)}">Detalle</button>
        ${job.has_artifact ? `<button type="button" data-open-job="${escapeHtml(job.id)}">Abrir resultado</button>` : ""}
      </div>
    </div>
  `).join("");
}

function formatTime(iso) {
  if (!iso) return "";
  return new Intl.DateTimeFormat("es-MX", { hour: "numeric", minute: "2-digit" }).format(new Date(iso));
}

function nodeName(node) {
  return ({ estate: "ESTATE", models: "MODELOS", soft: "SOFT" })[node] || String(node).toUpperCase();
}

function jobStatus(value) {
  return ({ queued: "en espera", running: "en proceso", completed: "completada", failed: "falló", interrupted: "interrumpida" })[value] || value;
}

async function refresh(silent = true) {
  try {
    renderBootstrap(await api("/api/bootstrap"));
  } catch (error) {
    renderBridgeUnavailable();
    if (!silent) toast(error.message, true);
  }
}

async function launchJob(operation, params = {}, confirmation = null) {
  try {
    const job = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ operation, params, confirmation }),
    });
    toast(`${job.label}: tarea iniciada.`);
    closeDialogs();
    await refresh();
    ensurePolling();
    return job;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function ensurePolling() {
  clearInterval(state.poll);
  state.poll = setInterval(async () => {
    await refresh();
    const active = state.bootstrap?.jobs?.some(job => ["queued", "running"].includes(job.status));
    if (!active) clearInterval(state.poll);
  }, 1800);
}

function closeDialogs() {
  $$('dialog[open]').forEach(dialog => dialog.close());
}

function requestConfirmation(operationKey) {
  const operation = state.operations.get(operationKey);
  if (!operation) return;
  state.pendingConfirmation = {
    kind: "operation",
    key: operationKey,
    phrase: operation.confirmation,
  };
  $("#confirm-title").textContent = operation.label;
  $("#confirm-copy").textContent = operation.description;
  $("#confirm-phrase").textContent = operation.confirmation;
  $("#confirm-input").value = "";
  $("#confirm-submit").disabled = true;
  closeDialogs();
  $("#confirm-dialog").showModal();
  $("#confirm-input").focus();
}

function requestEstateDisconnect() {
  state.pendingConfirmation = {
    kind: "estate-disconnect",
    phrase: "EXPULSAR USB",
  };
  $("#confirm-title").textContent = "Desconectar y expulsar Estate";
  $("#confirm-copy").textContent = "Alpha Go se cerrará y macOS expulsará de forma segura todo el dispositivo USB que contiene el Estate.";
  $("#confirm-phrase").textContent = "EXPULSAR USB";
  $("#confirm-input").value = "";
  $("#confirm-submit").disabled = true;
  closeDialogs();
  $("#confirm-dialog").showModal();
  $("#confirm-input").focus();
}

document.addEventListener("click", async event => {
  const closeButton = event.target.closest(".dialog-close");
  if (closeButton) {
    closeButton.closest("dialog")?.close();
    return;
  }

  const dialogButton = event.target.closest("[data-dialog]");
  if (dialogButton) {
    if (dialogButton.dataset.dialog === "model-dialog" && !state.segmentSetup) {
      await loadSegmentSetup($("#segments-template").value);
    }
    $("#" + dialogButton.dataset.dialog).showModal();
    return;
  }

  const operationButton = event.target.closest("[data-operation]");
  if (operationButton) {
    event.preventDefault();
    await launchJob(operationButton.dataset.operation);
    return;
  }

  const confirmButton = event.target.closest("[data-confirm-operation]");
  if (confirmButton) {
    event.preventDefault();
    requestConfirmation(confirmButton.dataset.confirmOperation);
    return;
  }

  const openButton = event.target.closest("[data-open]");
  if (openButton) {
    try {
      await api("/api/open", { method: "POST", body: JSON.stringify({ target: openButton.dataset.open }) });
      toast("Se abrió en su propia aplicación.");
    } catch (error) { toast(error.message, true); }
    return;
  }

  const logButton = event.target.closest("[data-log]");
  if (logButton) {
    const jobId = logButton.dataset.log;
    const job = state.bootstrap.jobs.find(item => item.id === jobId);
    $("#log-title").textContent = job?.label || "Registro de actividad";
    $("#log-content").textContent = "Cargando…";
    $("#log-dialog").showModal();
    try {
      const payload = await api(`/api/jobs/${jobId}/log`);
      $("#log-content").textContent = payload.log || "Todavía no hay información.";
    } catch (error) { $("#log-content").textContent = error.message; }
    return;
  }

  const openJob = event.target.closest("[data-open-job]");
  if (openJob) {
    try {
      await api(`/api/jobs/${openJob.dataset.openJob}/open`, { method: "POST", body: "{}" });
      toast("Se abrió el resultado terminado.");
    } catch (error) { toast(error.message, true); }
  }
});

$("#launch-alpha").addEventListener("click", async () => {
  const button = $("#launch-alpha");
  const destination = window.open("about:blank", "alpha-go");
  if (destination) {
    destination.document.title = "Abriendo Alpha Go…";
    destination.document.body.textContent = "Abriendo Alpha Go…";
  }
  button.disabled = true;
  button.querySelector("span").textContent = "Iniciando Alpha…";
  try {
    const payload = await api("/api/alpha/launch", { method: "POST", body: "{}" });
    if (destination) destination.location.replace(payload.url);
    else window.location.assign(payload.url);
    toast("Alpha Go abierto.");
    await refresh();
  } catch (error) {
    if (destination) destination.close();
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Abrir Alpha";
  }
});

$("#estate-toggle").addEventListener("click", async () => {
  const button = $("#estate-toggle");
  if (button.dataset.mode === "disconnect") {
    requestEstateDisconnect();
    return;
  }
  button.disabled = true;
  button.textContent = "Buscando USB…";
  try {
    const payload = await api("/api/estate/connect", { method: "POST", body: "{}" });
    toast(payload.message || "Estate conectado.");
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("#segments-template").addEventListener("change", event => loadSegmentSetup(event.target.value));
$("#metric-search").addEventListener("input", renderMetricList);
$("#metric-list").addEventListener("change", event => {
  if (!event.target.matches('input[type="checkbox"]')) return;
  if (event.target.checked) state.segmentSetup.selectedMetrics.add(event.target.value);
  else state.segmentSetup.selectedMetrics.delete(event.target.value);
  renderMetricList();
});
$("#model-form").addEventListener("submit", async event => {
  event.preventDefault();
  const job = await launchSegmentJob();
  if (job) closeDialogs();
});

$("#confirm-input").addEventListener("input", event => {
  const pending = state.pendingConfirmation;
  $("#confirm-submit").disabled = !pending || event.target.value !== pending.phrase;
});
$("#confirm-form").addEventListener("submit", async event => {
  event.preventDefault();
  const pending = state.pendingConfirmation;
  if (!pending) return;
  if (pending.kind === "estate-disconnect") {
    try {
      const payload = await api("/api/estate/disconnect", {
        method: "POST",
        body: JSON.stringify({ confirmation: $("#confirm-input").value }),
      });
      closeDialogs();
      toast(payload.message || "USB expulsado de forma segura.");
      await refresh();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const operation = state.operations.get(pending.key);
  if (!operation) return;
  await launchJob(operation.key, {}, $("#confirm-input").value);
});

$("#refresh-state").addEventListener("click", () => refresh(false));
$$("dialog").forEach(dialog => dialog.addEventListener("click", event => {
  if (event.target === dialog) dialog.close();
}));

refresh(false).then(ensurePolling);
