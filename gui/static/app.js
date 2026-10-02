"use strict";

const $ = (id) => document.getElementById(id);
const api = (path, opts) => fetch(path, opts).then(async (r) => {
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
});

const state = {
  project: null,
  runs: [],        // [{name, clips}]
  recut: { run: null, clip: null, words: [] },
};

// --------------------------------------------------------------------------- //
// Toast
// --------------------------------------------------------------------------- //
function toast(msg, isErr) {
  let t = $("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = ""), 3200);
}

// --------------------------------------------------------------------------- //
// Tabs
// --------------------------------------------------------------------------- //
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  const tab = btn.dataset.tab;
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + tab));
  if (tab === "review") loadRun($("reviewRun").value);
});

// --------------------------------------------------------------------------- //
// Console + job streaming
// --------------------------------------------------------------------------- //
let currentJob = null;
const consoleEl = $("console");
$("consoleToggle").addEventListener("click", () => {
  consoleEl.classList.toggle("collapsed");
  $("consoleToggle").textContent = consoleEl.classList.contains("collapsed") ? "▲" : "▼";
});
$("cancelJobBtn").addEventListener("click", () => {
  if (currentJob) api(`/api/jobs/${currentJob}/cancel`, { method: "POST" }).catch(() => {});
});

function setStatus(kind, text) {
  const b = $("consoleStatus");
  b.className = "badge " + kind;
  b.textContent = text;
}

function runJob(promise, label, onDone) {
  consoleEl.classList.remove("collapsed");
  $("consoleToggle").textContent = "▼";
  $("consoleBody").textContent = "";
  $("consoleLabel").textContent = label;
  setStatus("running", "running");
  $("cancelJobBtn").hidden = false;

  promise.then((res) => {
    currentJob = res.job;
    const es = new EventSource(`/api/jobs/${res.job}/stream`);
    const body = $("consoleBody");
    es.onmessage = (ev) => {
      let line = JSON.parse(ev.data);
      if (line.startsWith("[gui] __DONE__")) {
        const rc = parseInt(line.split("exit=")[1], 10);
        es.close();
        currentJob = null;
        $("cancelJobBtn").hidden = true;
        if (rc === 0) { setStatus("ok", "done"); toast(label + " — done"); }
        else { setStatus("fail", "exit " + rc); toast(label + " failed (exit " + rc + ")", true); }
        if (onDone) onDone(rc);
        return;
      }
      body.textContent += line + "\n";
      body.scrollTop = body.scrollHeight;
    };
    es.onerror = () => { es.close(); setStatus("fail", "stream lost"); $("cancelJobBtn").hidden = true; };
  }).catch((err) => {
    setStatus("fail", "error");
    $("consoleBody").textContent += "[gui] " + err.message + "\n";
    toast(err.message, true);
    $("cancelJobBtn").hidden = true;
  });
}

// --------------------------------------------------------------------------- //
// Projects
// --------------------------------------------------------------------------- //
async function loadProjects(selectName) {
  const projects = await api("/api/projects");
  const sel = $("projectSelect");
  sel.innerHTML = "";
  projects.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.name; o.textContent = p.name;
    sel.appendChild(o);
  });
  if (projects.length === 0) { state.project = null; return; }
  sel.value = selectName && projects.find((p) => p.name === selectName) ? selectName : projects[0].name;
  await selectProject(sel.value);
}

async function selectProject(name) {
  state.project = name;
  const p = await api("/api/projects/" + encodeURIComponent(name));
  state.runs = p.runs;
  // source chips
  const chips = $("sourceChips");
  chips.innerHTML = "";
  p.sources.forEach((s) => {
    const c = document.createElement("span");
    c.className = "chip"; c.textContent = "📹 " + s;
    c.onclick = () => ($("runSource").value = "source/" + s);
    chips.appendChild(c);
  });
  // run dropdowns
  fillRunSelects(p.runs);
  // settings
  $("cfgText").value = p.config;
  $("briefText").value = p.brief;
}

function fillRunSelects(runs) {
  ["reviewRun", "recutRun", "uploadRun"].forEach((id) => {
    const sel = $(id);
    const prev = sel.value;
    sel.innerHTML = "";
    runs.forEach((r) => {
      const o = document.createElement("option");
      o.value = r.name; o.textContent = `${r.name} (${r.clips} clips)`;
      sel.appendChild(o);
    });
    if (runs.find((r) => r.name === prev)) sel.value = prev;
  });
}

$("projectSelect").addEventListener("change", (e) => selectProject(e.target.value));
$("refreshBtn").addEventListener("click", () => loadProjects(state.project));

$("newProjectBtn").addEventListener("click", () => {
  const name = prompt("New project name:");
  if (!name) return;
  runJob(api("/api/projects", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }), "init " + name, () => loadProjects(name));
});

// --------------------------------------------------------------------------- //
// Browse (native file dialog via pywebview, fallback to manual entry)
// --------------------------------------------------------------------------- //
$("browseBtn").addEventListener("click", async () => {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_video) {
    const path = await window.pywebview.api.pick_video();
    if (path) $("runSource").value = path;
  } else {
    toast("Type the file path or paste a URL (browse needs the desktop app).");
  }
});

function needProject() {
  if (!state.project) { toast("Create or select a project first.", true); return false; }
  return true;
}

function runPayload() {
  return {
    project: state.project,
    source: $("runSource").value,
    name: $("runName").value,
    n: $("runN").value,
    render_top: $("runTop").value,
    formats: $("runFormats").value,
  };
}

$("runBtn").addEventListener("click", () => {
  if (!needProject()) return;
  runJob(api("/api/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(runPayload()),
  }), "run · " + state.project, () => selectProject(state.project));
});

$("candidatesBtn").addEventListener("click", () => {
  if (!needProject()) return;
  const p = runPayload();
  runJob(api("/api/candidates", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(p),
  }), "candidates · " + state.project);
});

// --------------------------------------------------------------------------- //
// Review
// --------------------------------------------------------------------------- //
$("reviewRun").addEventListener("change", (e) => loadRun(e.target.value));

function reveal(path) {
  api("/api/reveal", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  }).catch((err) => toast(err.message, true));
}

$("openRunFolder").addEventListener("click", () => {
  const run = $("reviewRun").value;
  if (!state.project || !run) { toast("Pick a run.", true); return; }
  reveal(`projects/${state.project}/output/${run}/clips`);
});

async function loadRun(run) {
  const grid = $("clipGrid");
  if (!run || !state.project) { grid.innerHTML = '<p class="hint">No run selected.</p>'; return; }
  grid.innerHTML = '<p class="hint">Loading…</p>';
  let data;
  try {
    data = await api(`/api/projects/${encodeURIComponent(state.project)}/runs/${encodeURIComponent(run)}`);
  } catch (err) { grid.innerHTML = `<p class="hint">${err.message}</p>`; return; }
  grid.innerHTML = "";
  if (!data.clips.length) { grid.innerHTML = '<p class="hint">No clips in this run.</p>'; return; }
  data.clips.forEach((c) => grid.appendChild(clipCard(c)));
}

function clipCard(c) {
  const card = document.createElement("div");
  card.className = "clip-card";
  const files = c._files || {};
  const firstFmt = Object.keys(files)[0];
  const videoPath = firstFmt ? files[firstFmt] : null;

  const thumb = document.createElement("div");
  thumb.className = "clip-thumb";
  if (c._thumb) {
    const img = document.createElement("img");
    img.src = "/api/file?path=" + encodeURIComponent(c._thumb);
    thumb.appendChild(img);
  } else {
    thumb.innerHTML = '<span class="noimg">no thumbnail</span>';
  }
  if (videoPath) {
    const play = document.createElement("span"); play.className = "play"; play.textContent = "▶";
    thumb.appendChild(play);
    thumb.onclick = () => openVideo(videoPath);
  }

  const len = (c.end - c.start).toFixed(1);
  const meta = document.createElement("div");
  meta.className = "clip-meta";
  meta.innerHTML = `
    <p class="clip-title">#${c.clip} · ${escapeHtml(c.title || "(untitled)")}</p>
    <div class="clip-sub"><span>${fmtTime(c.start)}–${fmtTime(c.end)} · ${len}s</span>
      <span class="badge-score">${(c.score ?? 0).toFixed ? (c.score ?? 0).toFixed(1) : c.score}</span></div>
    <p class="clip-hook">${escapeHtml(c.hook || "")}</p>
    <div class="clip-sub" style="margin-top:8px"><span>${Object.keys(files).join(", ") || "not rendered"}</span></div>`;
  card.appendChild(thumb);
  card.appendChild(meta);
  if (videoPath) {
    const bar = document.createElement("div");
    bar.className = "clip-actions";
    const revealBtn = document.createElement("button");
    revealBtn.className = "ghost"; revealBtn.textContent = "📂 Reveal";
    revealBtn.onclick = () => reveal(videoPath);
    bar.appendChild(revealBtn);
    meta.appendChild(bar);
  }
  return card;
}

// --------------------------------------------------------------------------- //
// Video modal
// --------------------------------------------------------------------------- //
function openVideo(relPath) {
  $("modalVideo").src = "/api/file?path=" + encodeURIComponent(relPath);
  $("modal").hidden = false;
  $("modalVideo").play().catch(() => {});
}
$("modalClose").addEventListener("click", closeVideo);
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeVideo(); });
function closeVideo() { const v = $("modalVideo"); v.pause(); v.src = ""; $("modal").hidden = true; }

// --------------------------------------------------------------------------- //
// Recut
// --------------------------------------------------------------------------- //
$("recutLoadBtn").addEventListener("click", loadRecut);

async function loadRecut() {
  if (!needProject()) return;
  const run = $("recutRun").value, clip = parseInt($("recutClip").value, 10);
  if (!run) { toast("Pick a run.", true); return; }
  const data = await api(`/api/recut/sidecar?project=${encodeURIComponent(state.project)}&run=${encodeURIComponent(run)}&clip=${clip}`);
  state.recut = { run, clip, words: data.words };
  renderRecut(data.words, data.exists);
}

function renderRecut(words, exists) {
  const box = $("recutEditor");
  box.innerHTML = "";
  if (!words.length) {
    box.innerHTML = exists
      ? '<p class="hint">Transcript is empty.</p>'
      : '<p class="hint">No editable transcript yet. Click <b>Re-render clip</b> once — the pipeline will extract it, then reload here to edit and re-render.</p>';
    $("recutSaveBtn").disabled = true;
    return;
  }
  $("recutSaveBtn").disabled = false;
  words.forEach((w, i) => {
    const row = document.createElement("div");
    row.className = "word-row" + (hasCJK(w.word) ? " cjk" : "");
    row.innerHTML = `<span class="t">${(+w.start).toFixed(2)}</span><span class="t">${(+w.end).toFixed(2)}</span>`;
    const inp = document.createElement("input");
    inp.type = "text"; inp.value = w.word;
    inp.oninput = () => { state.recut.words[i].word = inp.value; row.classList.toggle("cjk", hasCJK(inp.value)); };
    row.appendChild(inp);
    box.appendChild(row);
  });
}

$("recutSaveBtn").addEventListener("click", async () => {
  const { run, clip, words } = state.recut;
  await api("/api/recut/sidecar", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: state.project, run, clip, words }),
  });
  toast("Transcript saved.");
});

$("recutRenderBtn").addEventListener("click", async () => {
  if (!needProject()) return;
  const run = $("recutRun").value, clip = parseInt($("recutClip").value, 10);
  if (!run) { toast("Pick a run.", true); return; }
  if (state.recut.run === run && state.recut.clip === clip && state.recut.words.length) {
    await api("/api/recut/sidecar", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: state.project, run, clip, words: state.recut.words }),
    });
  }
  runJob(api("/api/recut", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: state.project, run, clip, formats: $("recutFormats").value }),
  }), `recut · ${state.project} #${clip}`, () => selectProject(state.project));
});

// --------------------------------------------------------------------------- //
// Upload
// --------------------------------------------------------------------------- //
$("ytBtn").addEventListener("click", () => {
  if (!needProject()) return;
  const run = $("uploadRun").value;
  if (!run) { toast("Pick a run.", true); return; }
  runJob(api("/api/upload", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project: state.project, run, format: $("uploadFormat").value,
      privacy: $("ytPrivacy").value, account: $("ytAccount").value,
      clips: $("ytClips").value, every: $("ytEvery").value, dry_run: $("ytDry").checked,
    }),
  }), "youtube · " + state.project);
});

function tiktok(extra, label) {
  if (!needProject()) return;
  runJob(api("/api/tiktok", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: state.project, run: $("uploadRun").value,
      format: $("uploadFormat").value, dry_run: $("tkDry").checked, ...extra }),
  }), label);
}
$("tkAuthBtn").addEventListener("click", () => tiktok({ auth: true }, "tiktok auth"));
$("tkCodeBtn").addEventListener("click", () => tiktok({ code: $("tkCode").value }, "tiktok code"));
$("tkBtn").addEventListener("click", () => tiktok({}, "tiktok push"));

// --------------------------------------------------------------------------- //
// Settings
// --------------------------------------------------------------------------- //
$("cfgSave").addEventListener("click", async () => {
  if (!needProject()) return;
  await api(`/api/projects/${encodeURIComponent(state.project)}/config`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: $("cfgText").value }),
  });
  toast("config.toml saved.");
});
$("briefSave").addEventListener("click", async () => {
  if (!needProject()) return;
  await api(`/api/projects/${encodeURIComponent(state.project)}/brief`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: $("briefText").value }),
  });
  toast("brief.txt saved.");
});

// --------------------------------------------------------------------------- //
// Doctor
// --------------------------------------------------------------------------- //
$("doctorBtn").addEventListener("click", () => {
  runJob(api("/api/doctor", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: state.project }),
  }), "doctor");
});

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //
function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}
function hasCJK(s) { return /[　-ヿ㐀-鿿豈-﫿＀-￯]/.test(s || ""); }
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  if (e.key === "Enter") { e.preventDefault(); $("runBtn").click(); }
  else if (e.key.toLowerCase() === "n") { e.preventDefault(); $("newProjectBtn").click(); }
});

// Boot
loadProjects().catch((err) => toast("Load failed: " + err.message, true));
