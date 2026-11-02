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
let jobFiles = {};          // id -> file of the open job (summary for the preview)
let pollTimer = null;
let uploading = false;      // the selection stays until its upload succeeds: no second click meanwhile

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
  $("#start").disabled = !selected.length || uploading;
  $("#start").textContent = uploading ? "Caricamento…" : selected.length ? `Converti ${plural(selected.length, "file", "file")}` : "Converti";
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
drop.addEventListener("click", e => { if (!e.target.closest("label")) $("#pick-files").click(); });
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
// Short message at the bottom of the screen (errors of an action, confirmations): no blocking alert.
let toastTimer = null;
function toast(text, kind = "") {
  const t = $("#toast");
  ($("#preview").open ? $("#preview") : document.body).appendChild(t);  // above the open preview
  t.textContent = text;
  t.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast"; }, kind === "error" ? 6000 : 2500);
}

$("#start").onclick = async () => {
  const mode = document.querySelector("input[name=mode]:checked").value;
  const rag = $("#opt-rag").checked, split = $("#opt-split").checked, pages = $("#opt-pages").value.trim();
  const files = selected;
  // Asked now, on a click: a long conversion can then tell you when it is done (only if the tab is hidden).
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {});
  uploading = true; renderSelection();
  const status = $("#upload-status");
  let job = null, accepted = 0;
  try {
    const r = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode, rag, split, pages }) });
    if (!r.ok) throw new Error(errorText(await r.text()));
    job = await r.json();
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
      accepted += res.accepted.length;
      poll();
    }
    selected = selected.filter(s => !files.includes(s)); // keeps files added during the upload
    status.textContent = rejected.length ? `File non accettati: ${rejected.map(r => `${r.name} (${r.error})`).join("; ")}` : "";
  } catch (err) {
    status.textContent = `Errore: ${err.message}`;
  } finally {
    if (job && !accepted) { // nothing to convert: drop the empty job instead of "Caricamento in corso…" forever
      await fetch(`/api/jobs/${job.id}`, { method: "DELETE" }).catch(() => {});
      if (currentJob === job.id) { currentJob = null; clearTimeout(pollTimer); $("#job").classList.add("hidden"); }
      loadHistory();
    }
    uploading = false; renderSelection();
  }
};

// ---------------------------------------------------------------- job view
function openJob(id) {
  currentJob = id;
  $("#job").classList.remove("hidden");
  $("#job").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
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
    if (wasActive[job.id] && !active) { notifyDone(job); loadHistory(); }
    wasActive[job.id] = active > 0;
    pollTimer = setTimeout(poll, active ? 1000 : 5000);
  } catch { pollTimer = setTimeout(poll, 3000); }
}

const wasActive = {};
function notifyDone(job) {
  if (!document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
  const done = job.files.filter(f => f.state === "done"), failed = job.files.length - done.length;
  const check = done.reduce((a, f) => a + (f.summary?.review?.length || 0), 0);
  const body = [done.length ? plural(done.length, "documento pronto", "documenti pronti") : "",
    check ? plural(check, "pagina da controllare", "pagine da controllare") : "",
    failed ? plural(failed, "non convertito", "non convertiti") : ""].filter(Boolean).join(" · ");
  const n = new Notification(failed && !done.length ? "Stele: conversione non riuscita" : "Stele: conversione completata", { body });
  n.onclick = () => { window.focus(); n.close(); };
}

function renderJob(job) {
  const c = job.counts, n = job.files.length;
  const done = c.done || 0, failed = (c.error || 0) + (c.cancelled || 0);
  const active = (c.queued || 0) + (c.running || 0) + (c.uploading || 0);
  const pagesDone = job.files.reduce((a, f) => a + (f.state === "done" ? (f.summary?.pages || 0) : 0), 0);
  $("#job-title").textContent = !n ? "Caricamento in corso…" : active ? "Conversione in corso"
    : done ? "Conversione completata" : "Conversione non riuscita";
  jobFiles = Object.fromEntries(job.files.map(f => [f.id, f]));
  $("#job-summary").innerHTML = [
    `Modalità <b>${MODE[job.mode]}</b>` + [job.rag && "per RAG", job.split && "in capitoli", job.pages && `pagine ${esc(job.pages)}`]
      .filter(Boolean).map(o => ` · ${o}`).join(""),
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
    const mode = f.mode || job.mode;
    const actions = f.state === "done" ? `<button class="btn-sm" data-prev="${f.id}" data-name="${esc(f.rel_path)}">Anteprima</button>
      <a class="link" href="/api/files/${f.id}/zip">Scarica</a>` + (mode !== "advanced" && s.pageable ? `
      <button class="link" data-retry="${f.id}" data-mode="advanced">Riconverti in Avanzata</button>` : "")
      : f.state === "error" || f.state === "cancelled" ? `<button class="link" data-retry="${f.id}">Riprova</button>` : "";
    const notes = f.state !== "done" ? [] : [
      s.reused && "Già convertito: risultato riusato",
      s.chapters && plural(s.chapters, "capitolo", "capitoli"),
      s.rechecked_pages === 0 && "Nessuna pagina da rileggere con un motore più accurato",
      s.rechecked_pages > 0 && plural(s.rechecked_pages, "pagina riletta", "pagine rilette"),
      s.edited && "Corretto a mano",
    ].filter(Boolean);
    const review = s.review?.length ? `<div class="warn small">${plural(s.review.length, "pagina da controllare", "pagine da controllare")}</div>` : "";
    const detail = (f.state === "running" ? `<div class="muted small">${esc(f.stage)}</div>` : "")
      + (f.error && f.state !== "running" ? `<div class="err small">${esc(f.error)}</div>` : "")
      + (notes.length ? `<div class="muted small">${notes.map(esc).join(" · ")}</div>` : "") + (f.state === "done" ? review : "");
    const label = mode !== job.mode ? ` <span class="muted small">· ${MODE[mode]}</span>` : "";
    return `<tr><td class="name">${esc(f.rel_path)}${label}${detail}</td><td data-label="Stato">${badge(f.state)}</td>
      <td data-label="Pagine">${pages}</td><td data-label="Tempo">${f.elapsed != null ? f.elapsed + " s" : ""}</td><td class="actions">${actions}</td></tr>`;
  }).join("");
}

$("#files").addEventListener("click", async e => {
  const b = e.target.closest("[data-prev]"), retry = e.target.closest("[data-retry]");
  if (b) preview(b.dataset.prev, b.dataset.name);
  if (retry) {
    if (jobFiles[retry.dataset.retry]?.summary?.edited && !confirm(EDITED_WARNING)) return;
    retry.disabled = true;
    const r = await fetch(`/api/files/${retry.dataset.retry}/retry`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(retry.dataset.mode ? { mode: retry.dataset.mode } : {}) });
    if (!r.ok) toast(errorText(await r.text()), "error");
    poll();
  }
});
$("#job-zip").addEventListener("click", e => { if ($("#job-zip").classList.contains("disabled")) e.preventDefault(); });
$("#job-cancel").onclick = async () => { if (currentJob) { await fetch(`/api/jobs/${currentJob}/cancel`, { method: "POST" }); poll(); } };

// ---------------------------------------------------------------- preview
// PDFs and images: the original page next to the Markdown; page markers (RAG option) align them.
const EDITED_WARNING = "Questo documento è stato corretto a mano. La nuova versione non avrà le correzioni " +
  "(la versione attuale resta come precedente e si può ripristinare). Continuare?";
let page = { fid: null, n: 1, total: 1 };
let cur = null;  // the open document: { fid, name, file, md }
function showPage(n) {
  page.n = Math.min(Math.max(1, n), page.total);
  $("#page-img").src = `/api/files/${page.fid}/pages/${page.n}`;
  $("#page-num").textContent = `Pagina ${page.n} di ${page.total}`;
  $("#page-prev").disabled = page.n <= 1;
  $("#page-next").disabled = page.n >= page.total;
  $("#preview-frame").contentDocument?.getElementById(`pagina-${page.n}`)?.scrollIntoView();
}
function togglePage(on) {
  $("#preview-page").classList.toggle("hidden", !on);
  $("#preview-body").classList.toggle("split", on);
  $("#preview-page-toggle").setAttribute("aria-pressed", String(on));
  if (on) showPage(page.n);
}
$("#page-prev").onclick = () => showPage(page.n - 1);
$("#page-next").onclick = () => showPage(page.n + 1);
$("#preview-page-toggle").onclick = () => togglePage($("#preview-page").classList.contains("hidden"));
$("#preview-review").addEventListener("click", e => { const b = e.target.closest("[data-page]"); if (b) { togglePage(true); showPage(+b.dataset.page); } });

// One main view at a time: rendered document, Markdown source, editor or comparison.
function setView(v) {
  [["#preview-frame", "render"], ["#preview-src", "raw"], ["#preview-edit", "edit"], ["#preview-compare", "compare"]]
    .forEach(([id, name]) => $(id).classList.toggle("hidden", v !== name));
  $("#preview-raw").setAttribute("aria-pressed", String(v === "raw"));
  $("#preview-tools").classList.toggle("hidden", v === "edit");
  $("#edit-actions").classList.toggle("hidden", v !== "edit");
}

function renderFrame(md, fid) {
  $("#preview-frame").srcdoc = `<!doctype html><html><head><meta charset="utf-8">
    <style>body{font:15px/1.6 -apple-system,system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 20px;color:#1d2330}
    table{border-collapse:collapse;margin:12px 0;font-size:14px}td,th{border:1px solid #d5dbe3;padding:4px 8px;text-align:left}th{background:#f2f5f8}
    img{max-width:100%}pre{background:#f4f6f8;padding:10px;overflow:auto}dl.props{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;font-size:13px;color:#4a5566;border-bottom:1px solid #d5dbe3;padding-bottom:10px}
    dl.props dt{font-weight:600}dl.props dd{margin:0}
    blockquote{border-left:3px solid #c8d1dc;margin:8px 0;padding-left:12px;color:#4a5566}.wikilink{color:#0f6e64;border-bottom:1px dotted}
    @media (prefers-color-scheme: dark){body{background:#151a21;color:#e3e8ef}th{background:#232a33}td,th{border-color:#384250}pre{background:#202731}dl.props{color:#aab4c0}}</style>
    </head><body>${window.renderMarkdown(md, `${location.origin}/api/files/${fid}/`)}</body></html>`;
}

function renderFeedback() {
  const s = cur.file?.summary || {};
  const ask = s.pageable && cur.file.state === "done" && !cur.file.feedback;
  $("#preview-feedback").classList.toggle("hidden", !ask);
  $("#feedback-text").textContent = "Com'è venuto il risultato?";
  $("#feedback-buttons").classList.remove("hidden");
  $("#feedback-hint").classList.remove("hidden");
}

async function preview(fid, name, file = jobFiles[fid]) {
  fid = Number(fid);
  const r = await fetch(`/api/files/${fid}/markdown?full=1`);
  const md = await r.text();
  const s = (r.ok && file?.summary) || {};
  cur = { fid, name, file, md };
  page = { fid, n: 1, total: s.source_pages || s.pages || 1 };
  $("#preview-title").textContent = name;
  $("#preview-page-toggle").classList.toggle("hidden", !s.pageable);
  $("#preview-edit-btn").classList.toggle("hidden", !!s.chapters);
  $("#preview-compare-btn").classList.toggle("hidden", !s.previous);
  $("#preview-tools").querySelectorAll("button").forEach(b => { b.disabled = !r.ok; });
  togglePage(false);
  const review = s.review || [];
  $("#preview-review").classList.toggle("hidden", !review.length);
  $("#preview-review").innerHTML = review.length ? `<b>${plural(review.length, "pagina da controllare", "pagine da controllare")}</b><ul>` +
    review.map(p => `<li><button class="link" data-page="${p.page}">Pagina ${p.page}</button> ${esc(p.reason)}</li>`).join("") + "</ul>" : "";
  if (!r.ok) {
    $("#preview-feedback").classList.add("hidden");
    $("#preview-src").textContent = `Anteprima non disponibile: ${errorText(md) || r.statusText}`;
    setView("raw");
    $("#preview").showModal();
    return;
  }
  renderFeedback();
  $("#preview-src").textContent = md;
  renderFrame(md, fid);
  setView("render");
  $("#preview").showModal();
  if (s.pageable && innerWidth > 700) togglePage(true);
}

function closePreview() {
  if (!$("#preview-edit").classList.contains("hidden") && $("#preview-edit").value !== cur.md
      && !confirm("Le modifiche non salvate andranno perse. Chiudere?")) return false;
  $("#preview").close();
  return true;
}
$("#preview-close").onclick = closePreview;
$("#preview").addEventListener("cancel", e => { e.preventDefault(); closePreview(); });  // Esc
$("#preview-raw").onclick = () => setView($("#preview-src").classList.contains("hidden") ? "raw" : "render");

// Copy: the whole document (chapters included) as Markdown, to paste in a note, a chat or a mail.
$("#preview-copy").onclick = async () => {
  const b = $("#preview-copy");
  try {
    await navigator.clipboard.writeText(cur.md);
    toast("Markdown copiato negli appunti");
  } catch { toast("Copia non riuscita: il browser non permette di usare gli appunti", "error"); }
};

// Edit: correct the text by hand next to the original page.
$("#preview-edit-btn").onclick = () => { $("#preview-edit").value = cur.md; setView("edit"); $("#preview-edit").focus(); };
$("#edit-cancel").onclick = () => {
  if ($("#preview-edit").value !== cur.md && !confirm("Annullare le modifiche?")) return;
  setView("render");
};
$("#edit-save").onclick = async () => {
  const text = $("#preview-edit").value;
  $("#edit-save").disabled = true;
  try {
    const r = await fetch(`/api/files/${cur.fid}/markdown`, { method: "PUT", headers: { "Content-Type": "text/markdown; charset=utf-8" }, body: text });
    if (!r.ok) { toast(errorText(await r.text()), "error"); return; }
    cur.md = text;
    if (cur.file?.summary) cur.file.summary.edited = true;
    $("#preview-src").textContent = text;
    renderFrame(text, cur.fid);
    setView("render");
    toast("Correzioni salvate");
    poll();
  } finally { $("#edit-save").disabled = false; }
};
$("#preview-edit").addEventListener("keydown", e => {  // ⌘S saves, like in an editor
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); $("#edit-save").click(); }
});

// Rating: good = done; so-so = the doubtful pages are re-read; to improve = reconverted with the most accurate method.
$("#feedback-buttons").addEventListener("click", async e => {
  const b = e.target.closest("[data-rate]");
  if (!b) return;
  const rating = b.dataset.rate;
  if (rating !== "good" && cur.file?.summary?.edited && !confirm(EDITED_WARNING)) return;
  const r = await fetch(`/api/files/${cur.fid}/feedback`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rating }) });
  if (!r.ok) { toast(errorText(await r.text()), "error"); return; }
  cur.file.feedback = rating;
  $("#feedback-buttons").classList.add("hidden");
  $("#feedback-hint").classList.add("hidden");
  $("#feedback-text").textContent = rating === "good" ? "Grazie: risultato confermato."
    : "Nuova lettura avviata. Al termine apri di nuovo l'anteprima e usa «Confronta» per scegliere la versione migliore.";
  poll();
});

// Compare with the version replaced by the last reconversion; keep this one or go back.
$("#preview-compare-btn").onclick = async () => {
  const r = await fetch(`/api/files/${cur.fid}/compare`);
  if (!r.ok) { toast(errorText(await r.text()), "error"); return; }
  const c = await r.json();
  const side = (label, v) => `<div><b>${label}</b> ${MODE[v.mode] || ""} · ${plural(v.words, "parola", "parole")} · ${plural(v.review, "pagina da controllare", "pagine da controllare")}</div>`;
  const line = l => `<span class="${l.startsWith("@@") ? "hunk" : l[0] === "+" ? "add" : l[0] === "-" ? "del" : ""}">${esc(l.startsWith("@@") ? "⋯" : l)}</span>`;
  $("#compare-summary").innerHTML = side("Prima:", c.previous) + side("Adesso:", c.current);
  $("#compare-diff").innerHTML = c.diff.length ? c.diff.map(line).join("\n") : `<span class="muted">Il testo è identico.</span>`;
  setView("compare");
};
$("#compare-keep").onclick = () => setView("render");
$("#compare-restore").onclick = async () => {
  const r = await fetch(`/api/files/${cur.fid}/restore`, { method: "POST" });
  if (!r.ok) { toast(errorText(await r.text()), "error"); return; }
  toast("Ripristinata la versione precedente");
  const job = await (await fetch(`/api/jobs/${cur.file.job_id}`)).json();
  $("#preview").close();
  preview(cur.fid, cur.name, job.files.find(f => f.id === cur.fid));
  poll();
};

// ---------------------------------------------------------------- history
async function loadHistory() {
  try {
    const jobs = await (await fetch("/api/jobs")).json();
    $("#history").innerHTML = jobs.length ? jobs.map(j => {
      const c = j.counts, total = Object.values(c).reduce((a, b) => a + b, 0);
      return `<li><button class="link" data-job="${j.id}">${new Date(j.created * 1000).toLocaleString("it-IT")} · ${MODE[j.mode]} · ${plural(total, "documento", "documenti")}</button>
        <span class="small">${["running", "queued", "done", "error"].filter(s => c[s]).map(s => `${badge(s)} ${c[s]}`).join(" ")}</span>
        <button class="link" data-keep="${j.id}" data-on="${j.keep ? 0 : 1}" title="${j.keep ? "Torna a eliminarla dopo 7 giorni" : "Non eliminarla dopo 7 giorni"}">${j.keep ? "Conservata ✓" : "Conserva"}</button>
        <button class="link danger" data-del="${j.id}" title="Elimina file caricati e risultati">Elimina</button></li>`;
    }).join("") : `<li class="muted">Nessuna conversione per ora: i lavori che avvii compaiono qui.</li>`;
  } catch { /* server restarting */ }
}
$("#history").addEventListener("click", async e => {
  const open = e.target.closest("[data-job]"), del = e.target.closest("[data-del]"), keep = e.target.closest("[data-keep]");
  if (open) openJob(open.dataset.job);
  if (keep) {
    await fetch(`/api/jobs/${keep.dataset.keep}/keep`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ keep: keep.dataset.on === "1" }) });
    loadHistory();
  }
  if (del && confirm("Eliminare definitivamente i file caricati e i risultati di questa conversione?")) {
    await fetch(`/api/jobs/${del.dataset.del}`, { method: "DELETE" });
    if (currentJob === del.dataset.del) { currentJob = null; $("#job").classList.add("hidden"); }
    loadHistory();
  }
});

// ---------------------------------------------------------------- search
let searchTimer = null;
async function runSearch() {
  const q = $("#search").value.trim();
  if (!q) { $("#search-results").innerHTML = ""; return; }
  try {
    const hits = await (await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
    if (q !== $("#search").value.trim()) return;  // a newer search is on its way
    $("#search-results").innerHTML = hits.length ? hits.map(h => `<li><button class="link" data-hit="${h.id}" data-job="${h.job_id}" data-name="${esc(h.rel_path)}">${esc(h.rel_path)}</button>
      <span class="muted small">${new Date(h.created * 1000).toLocaleDateString("it-IT")}</span>
      <div>${esc(h.snippet).replace(/\u0002/g, "<mark>").replace(/\u0003/g, "</mark>")}</div></li>`).join("")
      : `<li class="muted">Nessun documento contiene queste parole.</li>`;
  } catch { /* server restarting */ }
}
$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 250); });
$("#search-results").addEventListener("click", async e => {
  const b = e.target.closest("[data-hit]");
  if (!b) return;
  openJob(b.dataset.job);
  const job = await (await fetch(`/api/jobs/${b.dataset.job}`)).json();
  preview(b.dataset.hit, b.dataset.name, job.files.find(f => String(f.id) === b.dataset.hit));
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
