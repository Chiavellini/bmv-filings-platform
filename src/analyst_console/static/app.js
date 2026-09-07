const state = {
  bootstrap: null,
  bridgeAvailable: false,
  operations: new Map(),
  pendingConfirmation: null,
  poll: null,
  segmentSetup: null,
  extractor: { setup: null, upload: null },
};
const MAX_UPLOAD_BYTES = 60 * 1024 * 1024;

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

function createMetricPicker({ search, list, count, summary }) {
  return { search: $(search), list: $(list), count: $(count), summary, metrics: [], selected: new Set() };
}

function renderMetricPicker(picker) {
  const search = picker.search.value.trim().toLocaleLowerCase("es-MX");
  const metrics = [...picker.metrics]
    .sort((a, b) => a.label.localeCompare(b.label, "es-MX"))
    .filter(metric => `${metric.label} ${sectionLabel(metric.section)}`.toLocaleLowerCase("es-MX").includes(search));
  picker.list.innerHTML = metrics.map(metric => `
    <label class="metric-choice">
      <input type="checkbox" value="${escapeHtml(metric.key)}"${picker.selected.has(metric.key) ? " checked" : ""}>
      <span><b>${escapeHtml(metric.label)}</b><small>${escapeHtml(sectionLabel(metric.section))}</small></span>
    </label>
  `).join("") || '<p class="empty-metrics">No hay métricas que coincidan con la búsqueda.</p>';
  const count = picker.selected.size;
  picker.count.textContent = `${count} seleccionada${count === 1 ? "" : "s"}`;
  if (picker.summary) picker.summary(count);
}

function bindMetricPicker(picker) {
  picker.search.addEventListener("input", () => renderMetricPicker(picker));
  picker.list.addEventListener("change", event => {
    if (!event.target.matches('input[type="checkbox"]')) return;
    if (event.target.checked) picker.selected.add(event.target.value);
    else picker.selected.delete(event.target.value);
    renderMetricPicker(picker);
  });
}

const segmentPicker = createMetricPicker({
  search: "#metric-search",
  list: "#metric-list",
  count: "#metric-selection-count",
  summary: count => {
    $("#model-summary").textContent = count
      ? `${count} métrica${count === 1 ? "" : "s"} · Hoja Segments`
      : "Selecciona al menos una métrica";
  },
});

const extractorPicker = createMetricPicker({
  search: "#extractor-metric-search",
  list: "#extractor-metric-list",
  count: "#extractor-metric-count",
  summary: () => updateExtractorSummary(),
});

function renderMetricList() {
  renderMetricPicker(segmentPicker);
}

function renderSegmentSetup(payload) {
  state.segmentSetup = payload;
  state.segmentSetup.selectedMetrics = presetMetricKeys(payload);
  segmentPicker.metrics = payload.metrics;
  segmentPicker.selected = state.segmentSetup.selectedMetrics;
  segmentPicker.search.value = "";
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
    ensurePolling(job.id, true);
    return job;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function customMetricLines() {
  const seen = new Set();
  return $("#extractor-custom").value.split(/\r?\n/)
    .map(line => line.replace(/\s+/g, " ").trim())
    .filter(line => {
      if (!line) return false;
      const marker = line.toLocaleLowerCase("es-MX");
      if (seen.has(marker)) return false;
      seen.add(marker);
      return true;
    });
}

function updateExtractorSummary() {
  const count = extractorPicker.selected.size + customMetricLines().length;
  const upload = state.extractor.upload;
  $("#extractor-summary").textContent = !upload
    ? "Sube un PDF para continuar"
    : count
      ? `${count} métrica${count === 1 ? "" : "s"} · ${upload.filename}`
      : "Elige al menos una métrica";
}

function extractorCompanies() {
  const select = $("#extractor-company");
  const selected = select.value;
  select.innerHTML = '<option value="">Genérica · sin configuración de empresa</option>'
    + (state.bootstrap?.companies?.segments || []).map(row =>
      `<option value="${escapeHtml(row.slug)}">${escapeHtml(row.name)}</option>`
    ).join("");
  if ([...select.options].some(option => option.value === selected)) select.value = selected;
}

async function loadExtractorSetup(company = "") {
  const generate = $("#generate-extract");
  generate.disabled = true;
  try {
    const payload = await api(`/api/segments/setup?company=${encodeURIComponent(company)}`);
    state.extractor.setup = payload;
    const valid = new Set(payload.metrics.map(metric => metric.key));
    extractorPicker.metrics = payload.metrics;
    extractorPicker.selected = new Set([...extractorPicker.selected].filter(key => valid.has(key)));
    extractorPicker.search.value = "";
    renderMetricPicker(extractorPicker);
  } catch (error) {
    toast(error.message, true);
  } finally {
    generate.disabled = false;
  }
}

function resetExtractorUpload(statusText = "Ningún archivo seleccionado.") {
  state.extractor.upload = null;
  $("#extractor-file").value = "";
  $("#extractor-file-status").textContent = statusText;
  $("#extractor-period").value = "";
  updateExtractorSummary();
}

async function uploadExtractorFile(file) {
  if (!file) {
    resetExtractorUpload();
    return;
  }
  const isPdf = /\.pdf$/i.test(file.name) || file.type === "application/pdf";
  if (!isPdf) {
    resetExtractorUpload("Elige un archivo PDF.");
    toast("Elige un archivo PDF.", true);
    return;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    resetExtractorUpload("El PDF supera el límite de 60 MB.");
    toast("El PDF supera el límite de 60 MB.", true);
    return;
  }
  const status = $("#extractor-file-status");
  const generate = $("#generate-extract");
  state.extractor.upload = null;
  status.textContent = `Subiendo ${file.name} (${formatSize(file.size)})…`;
  generate.disabled = true;
  updateExtractorSummary();
  try {
    const payload = await api(`/api/extractor/upload?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": "application/pdf" },
      body: file,
    });
    state.extractor.upload = payload;
    $("#extractor-period").value = payload.period_guess || "";
    status.textContent = `${payload.filename} · ${formatSize(payload.size)} · ${
      payload.period_guess ? `periodo detectado ${payload.period_guess}` : "escribe el periodo si lo conoces"
    }`;
  } catch (error) {
    resetExtractorUpload("No se pudo subir el archivo.");
    toast(error.message, true);
  } finally {
    generate.disabled = false;
    updateExtractorSummary();
  }
}

function collectExtractorRequest() {
  return {
    upload: state.extractor.upload?.upload || "",
    company: $("#extractor-company").value,
    metrics: [...extractorPicker.selected],
    custom_metrics: customMetricLines(),
    period: $("#extractor-period").value.trim().toUpperCase(),
    format: $('input[name="extractor-format"]:checked')?.value || "both",
    read_tables: $("#extractor-tables").checked,
  };
}

async function launchExtractorJob() {
  if (!state.extractor.upload) {
    toast("Primero sube un PDF.", true);
    return null;
  }
  const request = collectExtractorRequest();
  if (!request.metrics.length && !request.custom_metrics.length) {
    toast("Elige al menos una métrica o escribe una adicional.", true);
    return null;
  }
  try {
    const job = await api("/api/extractor/jobs", {
      method: "POST",
      body: JSON.stringify(request),
    });
    toast(`${job.label}: tarea iniciada.`);
    closeDialogs();
    resetExtractorUpload();
    $("#extractor-custom").value = "";
    extractorPicker.selected = new Set();
    renderMetricPicker(extractorPicker);
    await refresh();
    ensurePolling(job.id, true);
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
  const models = payload.nodes.models;
  status(
    $("#models-status"),
    models.extractor_latest_available
      ? `Último extracto ${models.extractor_updated}`
      : models.latest_available ? `Última hoja ${models.updated}` : "Listo",
    "ready",
  );
  const estateToggle = $("#estate-toggle");
  estateToggle.dataset.mode = payload.estate.connected ? "disconnect" : "connect";
  estateToggle.textContent = payload.estate.connected ? "Desconectar y expulsar" : "Conectar Estate";
  estateToggle.classList.toggle("danger", payload.estate.connected);
  $("#estate-copy").textContent = payload.estate.connected
    ? `USB ${payload.estate.volume_name} conectado. Ya puedes usar Alpha, modelos y matrices.`
    : "Conecta el USB para usar la biblioteca de reportes y sus índices.";
  modelCompanies();
  extractorCompanies();
}

function setLocalControls(enabled) {
  const selectors = [
    "#launch-alpha",
    "#estate-toggle",
    "[data-dialog]",
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
    ensurePolling(job.id);
    return job;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function ensurePolling(watchedJobId = null, openResult = false) {
  clearInterval(state.poll);
  state.poll = setInterval(async () => {
    await refresh();
    const watched = watchedJobId
      ? state.bootstrap?.jobs?.find(job => job.id === watchedJobId)
      : null;
    if (watched && !["queued", "running"].includes(watched.status)) {
      clearInterval(state.poll);
      if (watched.status === "completed") {
        if (openResult && watched.has_artifact) {
          try {
            await api(`/api/jobs/${watched.id}/open`, { method: "POST", body: "{}" });
            toast(watched.operation === "pdf_extract"
              ? "Observaciones extraídas y abiertas."
              : "Hoja de segmentos terminada y abierta en Excel.");
          } catch (error) { toast(error.message, true); }
        } else {
          toast(`${watched.label}: ${watched.message.toLocaleLowerCase("es-MX")}.`);
        }
      } else {
        toast(`${watched.label}: ${watched.message.toLocaleLowerCase("es-MX")}.`, true);
      }
      return;
    }
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
    if (dialogButton.dataset.dialog === "extractor-dialog" && !state.extractor.setup) {
      await loadExtractorSetup($("#extractor-company").value);
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
bindMetricPicker(segmentPicker);
$("#model-form").addEventListener("submit", async event => {
  event.preventDefault();
  const job = await launchSegmentJob();
  if (job) closeDialogs();
});

bindMetricPicker(extractorPicker);
$("#extractor-file").addEventListener("change", event => uploadExtractorFile(event.target.files?.[0] || null));
$("#extractor-company").addEventListener("change", event => loadExtractorSetup(event.target.value));
$("#extractor-custom").addEventListener("input", updateExtractorSummary);
$("#extractor-form").addEventListener("submit", async event => {
  event.preventDefault();
  await launchExtractorJob();
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

$$("dialog").forEach(dialog => dialog.addEventListener("click", event => {
  if (event.target === dialog) dialog.close();
}));

refresh(false).then(ensurePolling);
