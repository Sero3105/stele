"use strict";
const $ = s => document.querySelector(s);
const fmtBytes = b => b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : b < 1073741824 ? `${(b / 1048576).toFixed(1)} MB` : `${(b / 1073741824).toFixed(2)} GB`;
const GB = 1073741824;
const STATE = { queued: "In attesa", uploading: "Caricamento", running: "In elaborazione", done: "Completato", error: "Errore", cancelled: "Annullato" };
const MODE = { auto: "Automatica", fast: "Veloce", advanced: "Avanzata" };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const NA = "Non disponibile";

let selected = [];          // [{file, path}]
let currentJob = null;
let pollTimer = null;

// ---------------------------------------------------------------- selection
function addFiles(items) {
  const seen = new Set(selected.map(s => s.path));
  for (const it of items) {
    if (it.path.split("/").some(p => p.startsWith("."))) continue; // .DS_Store etc.
    if (!seen.has(it.path)) { selected.push(it); seen.add(it.path); }
  }
  renderSelection();
}

function renderSelection() {
  $("#selection").classList.toggle("hidden", !selected.length);
  $("#start").disabled = !selected.length;
  const total = selected.reduce((a, s) => a + s.file.size, 0);
  $("#sel-count").textContent = `${selected.length} file selezionati · ${fmtBytes(total)}`;
  const types = {};
  for (const s of selected) { const e = (s.path.split(".").pop() || "?").toLowerCase(); types[e] = (types[e] || 0) + 1; }
  $("#sel-types").innerHTML = Object.entries(types).sort((a, b) => b[1] - a[1]).map(([e, n]) => `<li>${esc(e.toUpperCase())} · ${n}</li>`).join("");
}

$("#pick-files").onchange = e => { addFiles([...e.target.files].map(f => ({ file: f, path: f.name }))); e.target.value = ""; };
$("#pick-folder").onchange = e => { addFiles([...e.target.files].map(f => ({ file: f, path: f.webkitRelativePath || f.name }))); e.target.value = ""; };
$("#sel-clear").onclick = () => { selected = []; renderSelection(); };

// Drag & drop of files and whole folders (recursive through the entries API).
async function walk(entry, prefix) {
  if (entry.isFile) return [await new Promise((res, rej) => entry.file(f => res({ file: f, path: prefix + f.name }), rej))];
  const reader = entry.createReader();
  let all = [], batch;
  do { batch = await new Promise((res, rej) => reader.readEntries(res, rej)); all = all.concat(batch); } while (batch.length);
  const nested = await Promise.all(all.map(e => walk(e, prefix + entry.name + "/")));
  return nested.flat();
}
const drop = $("#drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, () => drop.classList.remove("over")));
drop.addEventListener("drop", async e => {
  e.preventDefault();
  const entries = [...e.dataTransfer.items].map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (entries.length) addFiles((await Promise.all(entries.map(en => walk(en, "")))).flat());
  else addFiles([...e.dataTransfer.files].map(f => ({ file: f, path: f.name })));
});
drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#pick-files").click(); } });

// ---------------------------------------------------------------- upload
function postForm(url, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = e => e.lengthComputable && onProgress(e.loaded);
    xhr.onload = () => xhr.status < 300 ? resolve(JSON.parse(xhr.responseText)) : reject(new Error(errorText(xhr.responseText) || xhr.statusText));
    xhr.onerror = () => reject(new Error("Connessione con l'applicazione persa"));
    xhr.send(form);
  });
}
function errorText(body) { try { return JSON.parse(body).detail; } catch { return body; } }

$("#start").onclick = async () => {
  const mode = document.querySelector("input[name=mode]:checked").value;
  const files = selected; selected = []; renderSelection();
  $("#start").disabled = true;
  const status = $("#upload-status");
  try {
    const r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
    if (!r.ok) throw new Error(errorText(await r.text()));
    const job = await r.json();
    openJob(job.id);
    const total = files.reduce((a, s) => a + s.file.size, 0);
    let sent = 0;
    const rejected = [];
    // Batches keep each request bounded; conversion starts as soon as the first batch lands.
    for (let i = 0; i < files.length;) {
      const form = new FormData();
      let bytes = 0, n = 0;
      while (i < files.length && n < 25 && (n === 0 || bytes + files[i].file.size < 256 * 1048576)) {
        form.append("files", files[i].file, files[i].path); bytes += files[i].file.size; i++; n++;
      }
      const base = sent;
      const res = await postForm(`/api/jobs/${job.id}/files`, form, l => { status.textContent = `Caricamento ${fmtBytes(base + l)} di ${fmtBytes(total)}…`; });
      sent += bytes;
      rejected.push(...res.rejected);
      poll();
    }
    status.textContent = rejected.length ? `File non accettati: ${rejected.map(r => `${r.name} (${r.error})`).join("; ")}` : "";
  } catch (err) {
    status.textContent = `Errore: ${err.message}`;
  }
};

// ---------------------------------------------------------------- job view
function openJob(id) {
  currentJob = id;
  $("#job").classList.remove("hidden");
  poll();
}

function badge(state) { return `<span class="badge ${state}">${STATE[state] || state}</span>`; }
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

async function poll() {
  clearTimeout(pollTimer);
  if (!currentJob) return;
  try {
    const r = await fetch(`/api/jobs/${currentJob}`);
    if (!r.ok) throw new Error();
    const job = await r.json();
    renderJob(job);
    const active = (job.counts.queued || 0) + (job.counts.running || 0) + (job.counts.uploading || 0);
    pollTimer = setTimeout(poll, active ? 1000 : 5000);
  } catch { pollTimer = setTimeout(poll, 3000); }
}

function renderJob(job) {
  const c = job.counts, n = job.files.length;
  const done = c.done || 0, failed = (c.error || 0) + (c.cancelled || 0);
  const active = (c.queued || 0) + (c.running || 0) + (c.uploading || 0);
  const pagesDone = job.files.reduce((a, f) => a + (f.state === "done" ? (f.summary?.pages || 0) : 0), 0);
  $("#job-title").textContent = !n ? "Caricamento in corso…" : active ? "Conversione in corso"
    : done ? "Conversione completata" : "Conversione non riuscita";
  $("#job-summary").innerHTML = [
    `Modalità <b>${MODE[job.mode]}</b>`,
    plural(n, "documento", "documenti"),
    done ? `${plural(done, "convertito", "convertiti")} · ${plural(pagesDone, "pagina", "pagine")}` : "",
    failed ? `<span class="err">${plural(failed, "non convertito", "non convertiti")}</span>` : "",
    active ? `${active} in corso o in attesa` : "",
  ].filter(Boolean).join(" · ");
  const partial = job.files.reduce((a, f) => a + (f.state === "running" && f.pages_total ? f.pages_done / f.pages_total : 0), 0);
  $("#job-bar").style.width = n ? `${100 * (done + failed + partial) / n}%` : "0";
  const zip = $("#job-zip");
  const ready = !active && done > 0;
  zip.classList.toggle("disabled", !ready);
  zip.setAttribute("aria-disabled", String(!ready));
  zip.href = ready ? `/api/jobs/${job.id}/zip` : "#";
  zip.textContent = ready ? (done === 1 ? "Scarica ZIP" : `Scarica ZIP (${done} documenti)`) : "Scarica ZIP";
  zip.title = ready ? "" : active ? "Disponibile al termine della conversione" : "Nessun documento convertito";
  const msg = $("#job-message");
  if (n && !active && !done) {
    msg.className = "message error"; msg.textContent = "Nessun documento è stato convertito. Controlla i messaggi di errore qui sotto.";
  } else if (!active && failed) {
    msg.className = "message warn"; msg.textContent = `${plural(failed, "documento non è stato convertito", "documenti non sono stati convertiti")}: lo ZIP contiene solo quelli riusciti e un README con l'elenco degli errori.`;
  } else { msg.className = "message hidden"; }
  $("#job-cancel").disabled = !active;
  $("#job-cancel").classList.toggle("hidden", !active);
  $("#files").innerHTML = job.files.map(f => {
    const s = f.summary || {};
    const pages = f.state === "done" ? (s.pages ?? "") : f.pages_total ? `${f.pages_done}/${f.pages_total}` : "";
    const actions = f.state === "done" ? `<button class="link" data-prev="${f.id}" data-name="${esc(f.rel_path)}">Anteprima</button>
      <a class="link" href="/api/files/${f.id}/zip">Scarica</a>` : "";
    const detail = f.state === "running" ? `<div class="muted small">${esc(f.stage)}</div>` : f.error ? `<div class="err small">${esc(f.error)}</div>` : "";
    return `<tr><td class="name">${esc(f.rel_path)}${detail}</td><td data-label="Stato">${badge(f.state)}</td>
      <td data-label="Pagine">${pages}</td><td data-label="Tempo">${f.elapsed != null ? f.elapsed + " s" : ""}</td><td class="actions">${actions}</td></tr>`;
  }).join("");
}

$("#files").addEventListener("click", e => { const b = e.target.closest("[data-prev]"); if (b) preview(b.dataset.prev, b.dataset.name); });
$("#job-zip").addEventListener("click", e => { if ($("#job-zip").classList.contains("disabled")) e.preventDefault(); });
$("#job-cancel").onclick = async () => { if (currentJob) { await fetch(`/api/jobs/${currentJob}/cancel`, { method: "POST" }); poll(); } };

// ---------------------------------------------------------------- preview
async function preview(fid, name) {
  const r = await fetch(`/api/files/${fid}/markdown`);
  const md = await r.text();
  $("#preview-title").textContent = name;
  $("#preview-src").textContent = md;
  $("#preview-src").classList.add("hidden");
  $("#preview-frame").classList.remove("hidden");
  $("#preview-frame").srcdoc = `<!doctype html><html><head><meta charset="utf-8">
    <style>body{font:15px/1.6 -apple-system,system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 20px;color:#1d2330}
    table{border-collapse:collapse;margin:12px 0;font-size:14px}td,th{border:1px solid #d5dbe3;padding:4px 8px;text-align:left}th{background:#f2f5f8}
    img{max-width:100%}pre{background:#f4f6f8;padding:10px;overflow:auto}dl.props{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;font-size:13px;color:#4a5566;border-bottom:1px solid #d5dbe3;padding-bottom:10px}
    dl.props dt{font-weight:600}dl.props dd{margin:0}
    blockquote{border-left:3px solid #c8d1dc;margin:8px 0;padding-left:12px;color:#4a5566}
    @media (prefers-color-scheme: dark){body{background:#151a21;color:#e3e8ef}th{background:#232a33}td,th{border-color:#384250}pre{background:#202731}dl.props{color:#aab4c0}}</style>
    </head><body>${window.renderMarkdown(md, `${location.origin}/api/files/${fid}/`)}</body></html>`;
  $("#preview").showModal();
}
$("#preview-close").onclick = () => $("#preview").close();
$("#preview-raw").onclick = () => {
  const raw = $("#preview-src").classList.toggle("hidden");
  $("#preview-frame").classList.toggle("hidden", !raw);
  $("#preview-raw").textContent = raw ? "Sorgente Markdown" : "Anteprima";
};

// ---------------------------------------------------------------- history
async function loadHistory() {
  try {
    const jobs = await (await fetch("/api/jobs")).json();
    $("#history").innerHTML = jobs.length ? jobs.map(j => {
      const c = j.counts, total = Object.values(c).reduce((a, b) => a + b, 0);
      return `<li><button class="link" data-job="${j.id}">${new Date(j.created * 1000).toLocaleString("it-IT")} · ${MODE[j.mode]} · ${plural(total, "documento", "documenti")}</button>
        <span class="small">${["running", "queued", "done", "error"].filter(s => c[s]).map(s => `${badge(s)} ${c[s]}`).join(" ")}</span>
        <button class="link danger" data-del="${j.id}" title="Elimina file caricati e risultati">Elimina</button></li>`;
    }).join("") : `<li class="muted">Nessuna conversione.</li>`;
  } catch { /* server restarting */ }
}
$("#history").addEventListener("click", async e => {
  const open = e.target.closest("[data-job]"), del = e.target.closest("[data-del]");
  if (open) openJob(open.dataset.job);
  if (del && confirm("Eliminare definitivamente i file caricati e i risultati di questa conversione?")) {
    await fetch(`/api/jobs/${del.dataset.del}`, { method: "DELETE" });
    if (currentJob === del.dataset.del) { currentJob = null; $("#job").classList.add("hidden"); }
    loadHistory();
  }
});

// ---------------------------------------------------------------- resources
const gb = b => b == null ? NA : `${(b / GB).toFixed(2)} GB`;
function gauge(bar, text, pct) {
  $(text).textContent = pct == null ? NA : `${Math.round(pct)}%`;
  $(bar).style.width = pct == null ? "0" : `${Math.min(100, pct)}%`;
}

async function loadSystem() {
  try {
    const s = await (await fetch("/api/system")).json();
    const used = Math.max(s.footprint, s.committed);
    $("#mem-text").textContent = `${(s.footprint / GB).toFixed(1)} / ${(s.budget / GB).toFixed(0)} GB`;
    const bar = $("#mem-bar");
    bar.style.width = `${Math.min(100, 100 * s.footprint / s.budget)}%`;
    bar.className = used > s.emergency ? "hot" : used > 0.6 * s.budget ? "warm" : "";
    $("#mem-emergency").style.left = `${100 * s.emergency / s.budget}%`;
    const rows = [
      ["Processo principale", gb(s.footprint_main)],
      ["Processi ausiliari", gb(s.footprint_children)],
      ["Modelli MLX (GPU)", s.mlx ? `${gb(s.mlx.active)} · picco ${gb(s.mlx.peak)}` : "non caricati"],
      ["Docling (GPU/MPS)", s.mps ? gb(s.mps.driver) : "non caricato"],
      ["Picco", gb(Math.max(s.peak, s.peak_main_kernel))],
      ["Budget", `${(s.budget / GB).toFixed(0)} GB · soglia ${(s.emergency / GB).toFixed(0)} GB`],
      ["Libera sul Mac", gb(s.system_available)],
    ];
    $("#mem-detail").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
    gauge("#gpu-bar", "#gpu-text", s.gpu_percent);
    gauge("#cpu-bar", "#cpu-text", s.cpu_percent);
    const q = s.queue;
    $("#queue").innerHTML = ["queued", "running", "done", "error"].map(k => `${badge(k)} ${q[k] || 0}`).join(" ") +
      `<div class="small muted">Lavori attivi: ${s.active_jobs.length}${s.waits ? ` · attese per memoria: ${s.waits}` : ""}</div>`;
    const missing = Object.values(s.engines).filter(e => !e.available);
    const warn = $("#engine-warning");
    warn.classList.toggle("hidden", !missing.length);
    warn.textContent = missing.length ? "Alcuni modelli non sono installati: la qualità sulle scansioni può essere ridotta (vedi README, «make models»)." : "";
  } catch { /* ignore transient errors */ }
}

$("#toggle-res").onclick = () => {
  const hidden = $("#resources").classList.toggle("hidden");
  $("#toggle-res").setAttribute("aria-expanded", String(!hidden));
};

loadHistory(); loadSystem();
setInterval(loadSystem, 2000);
setInterval(loadHistory, 5000);
