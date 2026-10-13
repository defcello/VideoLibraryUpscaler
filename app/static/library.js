// Library tab: directory browser (col 1), file list with conversion status
// (col 2), Create Job / Edit Settings forms (col 3) and predicted outputs
// (col 4). Workflows and per-directory settings files are resolved server
// side (see app/library.py); this file only renders them and collects edits.
// Relies on app.js globals: api, escapeHtml, presets, renderPresetDetails,
// STAGES, isStageSkipped, runningStageIndex, runningEta, refreshJobsNow.

const ACTIVE_JOB_STATUSES = ["pending", "running", "paused", "needs_review", "needs_restart"];
const SETTINGS_FILE_NAME = ".ai-remaster-pipeline.settings.json";
const FILTER_FIELDS = new Set(["show_regex", "hide_regex", "show_all", "show_hidden"]);

const lib = {
    roots: [],
    dir: null,
    parent: null,
    root: null,
    dirs: [],
    files: [],
    truncated: false,
    settings: null,         // GET /api/library/settings payload for lib.dir
    wfName: "Custom",
    selected: new Set(),
    completion: {},         // source path -> {completed, via, output}
    jobsByPath: {},         // normPath(source) -> most relevant job
    jobsSig: "",
    preview: [],
    esDraft: null,          // Edit Settings mode's working copy of the local settings file
};

const normPath = p => String(p || "").replace(/\//g, "\\").toLowerCase();
const $ = id => document.getElementById(id);

function debounce(fn, ms) {
    let t = null;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function jsonApi(path, method, body) {
    return api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

// ------------------------------------------------------------ col 1: browser

async function navigate(path) {
    let data;
    try {
        data = await api("/api/library/browse" + (path ? `?path=${encodeURIComponent(path)}` : ""));
    } catch (e) {
        alert("Couldn't open folder: " + e.message);
        $("lib-path").value = lib.dir || "";
        return;
    }
    const changed = normPath(data.path) !== normPath(lib.dir);
    Object.assign(lib, { dir: data.path, parent: data.parent, dirs: data.dirs, root: data.root });
    $("lib-path").value = data.path;
    try { localStorage.setItem("libraryDir", data.path); } catch (e) { /* ignore */ }
    renderBrowser();
    if (changed) {
        lib.selected.clear();
        exitEditMode();
    }
    await loadSettings();
    await loadFiles();
}

function renderBrowser() {
    const isRoot = lib.roots.some(r => normPath(r) === normPath(lib.dir));
    $("lib-add-root").disabled = isRoot;
    $("lib-remove-root").disabled = !isRoot || lib.roots.length <= 1;

    $("lib-roots").innerHTML = lib.roots.map(r => {
        const cls = ["lib-entry", "dir"];
        if (normPath(r) === normPath(lib.root)) cls.push("in-root");
        if (normPath(r) === normPath(lib.dir)) cls.push("current");
        return `<div class="${cls.join(" ")}" data-path="${escapeHtml(r)}" title="${escapeHtml(r)}">&#128450; ${escapeHtml(r)}</div>`;
    }).join("");

    let html = "";
    if (lib.parent) html += `<div class="lib-entry dir" data-path="${escapeHtml(lib.parent)}" title="${escapeHtml(lib.parent)}">&#11025; ..</div>`;
    html += lib.dirs.map(d =>
        `<div class="lib-entry dir" data-path="${escapeHtml(d.path)}" title="${escapeHtml(d.path)}">&#128193; ${escapeHtml(d.name)}${d.has_settings ? ' <span class="has-settings" title="Has a settings file">&#9881;</span>' : ""}</div>`
    ).join("");
    if (!lib.dirs.length) html += `<div class="lib-empty">no subfolders</div>`;
    $("lib-dirs").innerHTML = html;
    for (const el of document.querySelectorAll("#lib-roots .lib-entry, #lib-dirs .lib-entry")) {
        el.onclick = () => navigate(el.dataset.path);
    }
}

async function saveRoots(roots) {
    try {
        lib.roots = (await jsonApi("/api/library/roots", "PUT", { roots })).roots;
    } catch (e) {
        alert("Couldn't update roots: " + e.message);
        return;
    }
    await navigate(lib.dir);
}

// --------------------------------------------------- workflow form (col 2+3)

function wfInputs() { return document.querySelectorAll("[data-wf]"); }

function getFormWorkflow() {
    const wf = {};
    for (const el of wfInputs()) wf[el.dataset.wf] = el.type === "checkbox" ? el.checked : el.value;
    return wf;
}

function applyWorkflow(wf) {
    for (const el of wfInputs()) {
        const key = el.dataset.wf;
        if (!(key in wf)) continue;
        if (el.type === "checkbox") el.checked = !!wf[key]; else el.value = wf[key] ?? "";
    }
    $("test-crop-inputs").hidden = !$("test-crop-enabled").checked;
    if (!$("preset-details").hidden) renderPresetDetails();
}

function selectedWorkflow() {
    return lib.settings && lib.settings.workflows[lib.wfName];
}

function workflowDir() {
    const wf = selectedWorkflow();
    return (wf && wf.defined_in) || lib.dir;
}

function workflowDiff(form, base) {
    const diff = {};
    for (const k of lib.settings.workflow_fields) {
        if (k in form && base[k] !== form[k]) diff[k] = form[k];
    }
    return diff;
}

function isDirty() {
    const wf = selectedWorkflow();
    return !!wf && Object.keys(workflowDiff(getFormWorkflow(), wf)).length > 0;
}

function renderWorkflowSelect() {
    const sel = $("wf-select");
    const names = Object.keys(lib.settings.workflows);
    names.sort((a, b) => (a === "Custom") - (b === "Custom") || a.localeCompare(b));
    names.unshift(...names.splice(names.indexOf("Custom"), 1));
    sel.innerHTML = names.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
    sel.value = lib.wfName;
}

function renderWorkflowNote() {
    const wf = selectedWorkflow();
    if (!wf) return;
    const local = !!(lib.settings.local.workflows || {})[lib.wfName];
    let note = wf.defined_in ? `Defined in ${wf.defined_in}` : "App defaults";
    if (wf.defined_in && !local) note += " (inherited)";
    const dirty = isDirty();
    $("wf-note").innerHTML = escapeHtml(note) + (dirty ? ' · <span class="modified">modified</span>' : "");
    $("wf-rename-btn").disabled = $("wf-delete-btn").disabled = !local || lib.wfName === "Custom";
    $("wf-delete-btn").title = local ? "" : "Only workflows defined in this directory's settings file can be deleted here";
    $("wf-save-btn").disabled = !dirty;
    $("wf-save-note").textContent = local || !wf.defined_in
        ? "Saves to this directory's settings file"
        : "Saves an override into this directory's settings file";
}

async function loadSettings() {
    try {
        lib.settings = await api(`/api/library/settings?path=${encodeURIComponent(lib.dir)}`);
    } catch (e) {
        alert("Couldn't read settings: " + e.message);
        return;
    }
    const keepForm = isDirtyAgainstPrevious();
    const names = lib.settings.workflows;
    if (!keepForm && lib.settings.default_workflow !== "Custom") lib.wfName = lib.settings.default_workflow;
    else if (!names[lib.wfName]) lib.wfName = "Custom";
    renderWorkflowSelect();
    if (!keepForm) applyWorkflow(selectedWorkflow());
    lastAppliedWorkflow = { ...selectedWorkflow() };
    renderWorkflowNote();
}

// Unsaved form edits survive moving between folders -- compared against the
// workflow as it was last applied (the new folder's resolution may differ).
let lastAppliedWorkflow = null;
function isDirtyAgainstPrevious() {
    if (!lastAppliedWorkflow) return false;
    const form = getFormWorkflow();
    return Object.keys(form).some(k => k in lastAppliedWorkflow && lastAppliedWorkflow[k] !== form[k]);
}

function onWorkflowSelect() {
    if (isDirty() && !confirm("Discard unsaved changes to this workflow?")) {
        $("wf-select").value = lib.wfName;
        return;
    }
    lib.wfName = $("wf-select").value;
    applyWorkflow(selectedWorkflow());
    lastAppliedWorkflow = { ...selectedWorkflow() };
    onFormChange(null, true);
}

function onFormChange(key, reloadFiles) {
    renderWorkflowNote();
    if (reloadFiles || key === "include_subdirs") {
        loadFiles();
        return;
    }
    if (FILTER_FIELDS.has(key)) renderFiles();
    refreshStatusSoon();
    refreshPreviewSoon();
}

async function workflowAction(action, name, extra) {
    try {
        lib.settings = await jsonApi("/api/library/workflow", "POST", { dir: lib.dir, action, name, ...extra });
    } catch (e) {
        alert(`Couldn't ${action} workflow: ` + e.message);
        return false;
    }
    return true;
}

async function afterWorkflowEdit(newName) {
    lib.wfName = newName && lib.settings.workflows[newName] ? newName : "Custom";
    renderWorkflowSelect();
    applyWorkflow(selectedWorkflow());
    lastAppliedWorkflow = { ...selectedWorkflow() };
    renderBrowser();
    onFormChange(null, true);
    // The browser's settings-file markers / file list header may have changed.
    const b = await api(`/api/library/browse?path=${encodeURIComponent(lib.dir)}`).catch(() => null);
    if (b) { lib.dirs = b.dirs; renderBrowser(); }
}

async function saveWorkflowChanges() {
    if (await workflowAction("save", lib.wfName, { fields: getFormWorkflow() })) afterWorkflowEdit(lib.wfName);
}

async function saveWorkflowAs() {
    const name = $("wf-saveas-input").value.trim();
    if (!name) { alert("Enter a name for the new workflow."); return; }
    if (lib.settings.workflows[name] && !confirm(`"${name}" already exists here -- overwrite it in this directory's settings file?`)) return;
    if (await workflowAction("save", name, { fields: getFormWorkflow() })) {
        $("wf-saveas-input").value = "";
        afterWorkflowEdit(name);
    }
}

async function renameWorkflow() {
    const newName = $("wf-rename-input").value.trim();
    if (!newName) { alert("Enter the new name."); return; }
    if (await workflowAction("rename", lib.wfName, { new_name: newName })) {
        $("wf-rename-input").value = "";
        afterWorkflowEdit(newName);
    }
}

async function deleteWorkflow() {
    if (!confirm(`Delete workflow "${lib.wfName}" from ${lib.settings.file}?`)) return;
    const name = lib.wfName;
    if (await workflowAction("delete", name)) afterWorkflowEdit(name);  // falls back to an inherited one of the same name, if any
}

// ------------------------------------------------------------- col 2: files

async function loadFiles() {
    const dir = lib.dir;
    const recursive = $("include-subdirs").checked;
    $("lib-files").innerHTML = `<div class="lib-empty">loading…</div>`;
    let data;
    try {
        data = await api(`/api/library/files?path=${encodeURIComponent(dir)}&recursive=${recursive}`);
    } catch (e) {
        $("lib-files").innerHTML = `<div class="lib-empty">${escapeHtml(e.message)}</div>`;
        return;
    }
    if (dir !== lib.dir || recursive !== $("include-subdirs").checked) return;  // superseded
    lib.files = data.files;
    lib.truncated = data.truncated;
    lib.completion = {};
    renderFiles();
    refreshStatus();
}

function compileFilter(id) {
    const el = $(id);
    try {
        el.classList.remove("invalid");
        return new RegExp(el.value);
    } catch (e) {
        el.classList.add("invalid");
        el.title = e.message;
        return null;
    }
}

function visibleFiles() {
    const showRx = compileFilter("show-regex") || /.*/;
    const hideRx = compileFilter("hide-regex") || /(?!)/;
    const showAll = $("show-all").checked, showHidden = $("show-hidden").checked;
    const out = [];
    for (const f of lib.files) {
        if (!showAll && !f.is_video) continue;
        if (!showRx.test(f.name)) continue;
        const hidden = hideRx.test(f.name);
        if (hidden && !showHidden) continue;
        out.push({ ...f, hiddenByFilter: hidden });
    }
    return out;
}

const isSelectable = f => f.is_video && !f.hiddenByFilter;

function fileState(path) {
    const job = lib.jobsByPath[normPath(path)];
    if (job && job.status === "running") return { cls: "running", job };
    if (job && ACTIVE_JOB_STATUSES.includes(job.status)) return { cls: "pending", job };
    const c = lib.completion[path];
    if (c && c.completed) return { cls: "done", info: c };
    if (job && job.status === "failed") return { cls: "failed", job };
    return { cls: "none" };
}

// Segmented ring: one arc per stage this job actually runs, green when done,
// the running one filled by its progress -- same colors as the Queue's track.
function progressRing(job) {
    const stages = STAGES.map((s, i) => i).filter(i => !isStageSkipped(job, i));
    const lastDone = STAGES.indexOf(job.stage);
    const running = runningStageIndex(job);
    const pct = Math.max(0, Math.min(100, job.progress_percent ?? 0)) / 100;
    const n = stages.length, span = 360 / n, gap = n > 1 ? Math.min(8, span * 0.18) : 0.01;
    const R = 7, C = 9;
    const pt = a => { const r = (a - 90) * Math.PI / 180; return `${(C + R * Math.cos(r)).toFixed(2)} ${(C + R * Math.sin(r)).toFixed(2)}`; };
    const arc = (a0, a1, color) => a1 - a0 < 0.05 ? "" :
        `<path d="M ${pt(a0)} A ${R} ${R} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${pt(a1)}" stroke="${color}" />`;
    let paths = "";
    stages.forEach((i, k) => {
        const a0 = k * span + gap / 2, a1 = (k + 1) * span - gap / 2;
        if (i <= lastDone) paths += arc(a0, a1, "var(--green)");
        else if (i === running) paths += arc(a0, a1, "var(--border-strong)") + arc(a0, a0 + (a1 - a0) * pct, "var(--accent)");
        else paths += arc(a0, a1, "var(--border-strong)");
    });
    return `<svg class="ring" viewBox="0 0 18 18" width="16" height="16" fill="none" stroke-width="3">${paths}</svg>`;
}

function statusTitle(state) {
    if (state.cls === "running") {
        const { pct, framesText, etaText } = runningEta(state.job);
        return `Processing: ${STAGES[runningStageIndex(state.job)]} ${pct.toFixed(0)}%${framesText ? "; " + framesText : ""}${etaText ? "; stage ETA " + etaText : ""}`;
    }
    if (state.cls === "pending") return `In queue (${state.job.status.replace(/_/g, " ")})`;
    if (state.cls === "done") return `Done for this workflow (matched by ${state.info.via}): ${state.info.output}`;
    if (state.cls === "failed") return `Last job failed: ${state.job.error_message || ""}`.trim();
    return "Not processed by this workflow";
}

function renderFiles() {
    const list = $("lib-files");
    const files = visibleFiles();
    const selectable = new Set(files.filter(isSelectable).map(f => f.path));
    for (const p of [...lib.selected]) if (!selectable.has(p)) lib.selected.delete(p);

    const s = lib.settings;
    const exists = s && s.file_exists;
    let html = `<div class="lib-entry settings-row">
        <button class="icon-btn pencil" id="edit-settings-btn" title="Edit this directory's settings">&#9998;</button>
        <span class="${exists ? "" : "placeholder"}" title="${escapeHtml(s ? s.file : "")}">${SETTINGS_FILE_NAME}${exists ? "" : " (not created — using inherited/default settings)"}</span>
    </div>`;
    const recursive = $("include-subdirs").checked;
    for (const f of files) {
        const label = recursive ? f.rel : f.name;
        if (!f.is_video) {
            html += `<div class="lib-entry file other${f.hiddenByFilter ? " subdued" : ""}" title="${escapeHtml(f.path)}"><span class="cb-spacer"></span><span class="fname">${escapeHtml(label)}</span></div>`;
            continue;
        }
        const st = fileState(f.path);
        let extra = "";
        if (st.cls === "running") {
            const { etaText } = runningEta(st.job);
            extra = `${progressRing(st.job)}<span class="eta">${etaText ? "ETA " + escapeHtml(etaText) : escapeHtml(STAGES[runningStageIndex(st.job)])}</span>`;
        }
        const checked = lib.selected.has(f.path) ? "checked" : "";
        const disabled = f.hiddenByFilter ? "disabled" : "";
        html += `<div class="lib-entry file st-${st.cls}${f.hiddenByFilter ? " subdued" : ""}" data-path="${escapeHtml(f.path)}" title="${escapeHtml(f.path + "\n" + statusTitle(st))}">
            <input type="checkbox" ${checked} ${disabled}><span class="fname">${escapeHtml(label)}</span>${extra}</div>`;
    }
    if (!files.length) html += `<div class="lib-empty">no ${$("show-all").checked ? "" : "video "}files match</div>`;
    if (lib.truncated) html += `<div class="lib-empty">list truncated — too many files</div>`;
    const scroll = list.scrollTop;
    list.innerHTML = html;
    list.scrollTop = scroll;

    $("edit-settings-btn").onclick = enterEditMode;
    for (const row of list.querySelectorAll(".lib-entry.file[data-path]")) {
        const cb = row.querySelector("input");
        if (cb.disabled) continue;
        row.onclick = (e) => {
            if (e.target !== cb) cb.checked = !cb.checked;
            if (cb.checked) lib.selected.add(row.dataset.path); else lib.selected.delete(row.dataset.path);
            renderSelectAll(selectable);
            refreshPreviewSoon();
        };
    }
    renderSelectAll(selectable);
    const key = [...lib.selected].join("|");
    if (key !== lastSelectionKey) {
        lastSelectionKey = key;
        refreshPreviewSoon();
    }
}

let lastSelectionKey = null;

function renderSelectAll(selectable) {
    const all = $("lib-select-all");
    const n = lib.selected.size, total = selectable.size;
    all.checked = total > 0 && n === total;
    all.indeterminate = n > 0 && n < total;
    all.disabled = total === 0;
    $("lib-select-count").textContent = n ? `${n} of ${total} selected` : `Select all (${total})`;
}

function toggleSelectAll() {
    const selectable = visibleFiles().filter(isSelectable).map(f => f.path);
    const selectAll = lib.selected.size < selectable.length;
    lib.selected = new Set(selectAll ? selectable : []);
    renderFiles();
}

let statusSeq = 0;
async function refreshStatus() {
    const seq = ++statusSeq;
    const paths = lib.files.filter(f => f.is_video).map(f => f.path);
    if (!paths.length || !lib.settings) return;
    try {
        const res = await jsonApi("/api/library/status", "POST", {
            paths, workflow: getFormWorkflow(), workflow_dir: workflowDir(), workflow_name: lib.wfName,
        });
        if (seq !== statusSeq) return;
        lib.completion = res;
        renderFiles();
    } catch (e) { /* transient -- the next form change or job update retries */ }
}
const refreshStatusSoon = debounce(refreshStatus, 500);

// Live job overlay from the SSE stream (app.js connectStream).
window.libraryOnJobs = function (jobs) {
    const rank = j => j.status === "running" ? 3 : ACTIVE_JOB_STATUSES.includes(j.status) ? 2 : 1;
    const map = {};
    for (const j of jobs) {
        const k = normPath(j.original_nas_path);
        const cur = map[k];
        if (!cur || rank(j) > rank(cur) || (rank(j) === rank(cur) && j.updated_at > cur.updated_at)) map[k] = j;
    }
    const shown = new Set(lib.files.map(f => normPath(f.path)));
    const sig = Object.entries(map).filter(([k]) => shown.has(k))
        .map(([k, j]) => `${k}|${j.status}|${j.stage}|${Math.round(j.progress_percent || 0)}`).sort().join(";");
    const finished = Object.entries(map).some(([k, j]) => shown.has(k) && j.status === "done"
        && lib.jobsByPath[k] && lib.jobsByPath[k].status !== "done");
    lib.jobsByPath = map;
    if (sig !== lib.jobsSig) {
        lib.jobsSig = sig;
        renderFiles();
    }
    if (finished) refreshStatusSoon();
};

// ----------------------------------------------------------- col 4: outputs

let previewSeq = 0;
async function refreshPreview() {
    const seq = ++previewSeq;
    const order = visibleFiles().map(f => f.path).filter(p => lib.selected.has(p));
    const list = $("lib-outputs");
    if (!order.length) {
        lib.preview = [];
        list.innerHTML = `<div class="lib-empty">Select video files to preview their outputs.</div>`;
        $("add-to-queue").disabled = true;
        $("add-to-queue").textContent = "Add to Queue";
        return;
    }
    try {
        const res = await jsonApi("/api/library/preview", "POST", {
            paths: order, workflow: getFormWorkflow(), workflow_dir: workflowDir(), workflow_name: lib.wfName,
        });
        if (seq !== previewSeq) return;
        lib.preview = res;
    } catch (e) {
        if (seq === previewSeq) list.innerHTML = `<div class="lib-empty">${escapeHtml(e.message)}</div>`;
        return;
    }
    renderPreview();
}
const refreshPreviewSoon = debounce(refreshPreview, 250);

function renderPreview() {
    const full = $("show-full-path").checked;
    const list = $("lib-outputs");
    list.innerHTML = lib.preview.map(p => {
        if (p.error) {
            return `<div class="lib-entry out conflict" title="${escapeHtml(p.source + "\n" + p.error)}">${escapeHtml(p.error)} — ${escapeHtml(p.source.split(/[\\/]/).pop())}</div>`;
        }
        const name = full ? p.output_path : p.output_path.split(/[\\/]/).pop();
        const tip = [p.output_path, `from: ${p.source}`, p.conflict ? `CONFLICT: ${p.conflict}` : "", p.warning || ""].filter(Boolean).join("\n");
        return `<div class="lib-entry out ${p.conflict ? "conflict" : "predicted"}" title="${escapeHtml(tip)}">${escapeHtml(name)}</div>`;
    }).join("");
    const btn = $("add-to-queue");
    btn.disabled = !lib.preview.length || lib.preview.some(p => p.error);
    btn.textContent = `Add to Queue (${lib.preview.length})`;
}

async function addToQueue() {
    const conflicts = lib.preview.filter(p => p.conflict);
    if (conflicts.length && !confirm(`${conflicts.length} output(s) look like they'll conflict:\n\n`
        + conflicts.slice(0, 8).map(p => `${p.output_path.split(/[\\/]/).pop()} — ${p.conflict}`).join("\n")
        + `\n\nQueue anyway? (Existing files are never overwritten; the new output gets a " (2)" suffix.)`)) return;
    const btn = $("add-to-queue");
    btn.disabled = true;
    try {
        await jsonApi("/api/library/queue", "POST", {
            paths: lib.preview.map(p => p.source), workflow: getFormWorkflow(),
            workflow_dir: workflowDir(), workflow_name: lib.wfName,
        });
    } catch (e) {
        alert("Couldn't add to queue: " + e.message);
        btn.disabled = false;
        return;
    }
    lib.selected.clear();
    try { window.libraryOnJobs(await api("/api/jobs")); } catch (e) { /* SSE catches up */ }
    renderFiles();
    refreshJobsNow();
}

// ------------------------------------------------------ col 3: edit settings

function enterEditMode() {
    if (!lib.settings) return;
    lib.esDraft = JSON.parse(JSON.stringify(lib.settings.local || {}));
    $("create-job-mode").hidden = true;
    $("edit-settings-mode").hidden = false;
    renderEditSettings();
}

function exitEditMode() {
    lib.esDraft = null;
    $("create-job-mode").hidden = false;
    $("edit-settings-mode").hidden = true;
}

function renderEditSettings() {
    const s = lib.settings, d = lib.esDraft;
    $("es-path").textContent = s.file + (s.file_exists ? "" : "  (doesn't exist yet — saving creates it)");
    $("es-ignore-parent").checked = !!d.ignore_parent_settings;

    const wfs = d.workflows || {};
    const names = [...new Set(["Custom", ...Object.keys(s.workflows), ...Object.keys(wfs)])];
    $("es-default-wf").innerHTML = `<option value="">(inherit from parent)</option>`
        + names.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
    $("es-default-wf").value = d.default_workflow || "";

    const box = $("es-workflows");
    box.innerHTML = Object.keys(wfs).length ? "" : `<div class="hint" style="margin:6px 0">No workflows defined in this directory.</div>`;
    for (const [name, fields] of Object.entries(wfs)) {
        const card = document.createElement("div");
        card.className = "es-wf";
        card.innerHTML = `<div class="es-wf-head"><strong>${escapeHtml(name)}</strong>
            <button class="small danger" title="Remove this definition">Remove</button></div>
            <textarea spellcheck="false" rows="${Math.min(14, Object.keys(fields).length + 2)}"></textarea>
            <div class="es-err"></div>`;
        const ta = card.querySelector("textarea");
        ta.value = JSON.stringify(fields, null, 2);
        ta.oninput = () => {
            try {
                const v = JSON.parse(ta.value);
                if (typeof v !== "object" || Array.isArray(v) || v === null) throw new Error("must be a JSON object");
                const unknown = Object.keys(v).filter(k => !s.workflow_fields.includes(k));
                if (unknown.length) throw new Error("unknown field(s): " + unknown.join(", "));
                wfs[name] = v;
                ta.classList.remove("invalid");
                card.querySelector(".es-err").textContent = "";
            } catch (e) {
                ta.classList.add("invalid");
                card.querySelector(".es-err").textContent = e.message;
            }
        };
        card.querySelector("button").onclick = () => { delete wfs[name]; renderEditSettings(); };
        box.appendChild(card);
    }

    const inherited = Object.entries(s.workflows)
        .filter(([n, wf]) => wf.defined_in && normPath(wf.defined_in) !== normPath(s.dir) && !(n in wfs));
    const inh = $("es-inherited");
    inh.innerHTML = inherited.length ? inherited.map(([n, wf]) =>
        `<div class="es-inh"><span><strong>${escapeHtml(n)}</strong> — ${escapeHtml(wf.defined_in)}</span>
         <button class="small" data-name="${escapeHtml(n)}">Override here</button></div>`).join("")
        : (s.chain.length > 1 ? "Nothing inherited beyond the app defaults." : "This directory is the inheritance root.");
    for (const b of inh.querySelectorAll("button")) {
        b.onclick = () => { d.workflows = { ...wfs, [b.dataset.name]: {} }; renderEditSettings(); };
    }
}

async function saveEditSettings() {
    if (document.querySelector("#es-workflows textarea.invalid")) { alert("Fix the invalid workflow JSON first."); return; }
    const d = lib.esDraft;
    d.ignore_parent_settings = $("es-ignore-parent").checked;
    if (!d.ignore_parent_settings) delete d.ignore_parent_settings;
    const def = $("es-default-wf").value;
    if (def) d.default_workflow = def; else delete d.default_workflow;
    try {
        lib.settings = await jsonApi(`/api/library/settings?path=${encodeURIComponent(lib.dir)}`, "PUT", d);
    } catch (e) {
        alert("Couldn't save settings: " + e.message);
        return;
    }
    exitEditMode();
    lastAppliedWorkflow = null;
    await loadSettings();
    afterWorkflowEdit(lib.wfName);
}

async function revertEditSettings() {
    try {
        lib.settings = await api(`/api/library/settings?path=${encodeURIComponent(lib.dir)}`);
    } catch (e) {
        alert("Couldn't reload settings: " + e.message);
        return;
    }
    enterEditMode();
}

// ------------------------------------------------------------- insert menus

const INSERT_OPTIONS = {
    dir: [
        ["${dir-source}", "folder containing the source file"],
        ["${dir-workflow}", "folder whose settings file defines this workflow"],
        ["${dir-root}", "browser root containing the source file"],
        ["$$", "literal $"], ["${lb}", "literal {"], ["${rb}", "literal }"],
    ],
    name: [
        ["$1", "base filename group 1"], ["$2", "base filename group 2"], ["$3", "base filename group 3"],
        ["${workflow-tag}", "the workflow tag field"],
        ["${output-vresmode}", "output resolution, e.g. 1080p / 480i"],
        ["${ext}", "output extension (.mkv)"], ["${source-ext}", "source file's extension"],
        ["${workflow-name}", "selected workflow's name"],
        ["$$", "literal $"], ["${lb}", "literal {"], ["${rb}", "literal }"],
    ],
};

function initInsertMenus() {
    for (const sel of document.querySelectorAll(".insert-select")) {
        sel.innerHTML = `<option value="">Insert…</option>` + INSERT_OPTIONS[sel.dataset.kind]
            .map(([v, d]) => `<option value="${escapeHtml(v)}">${escapeHtml(v)} — ${escapeHtml(d)}</option>`).join("");
        sel.onchange = () => {
            const input = $(sel.dataset.target);
            if (!sel.value) return;
            const start = input.selectionStart ?? input.value.length, end = input.selectionEnd ?? input.value.length;
            input.setRangeText(sel.value, start, end, "end");
            input.focus();
            input.dispatchEvent(new Event("input", { bubbles: true }));
            sel.value = "";
        };
    }
}

// ------------------------------------------------------------------- init

async function initLibrary() {
    initInsertMenus();
    for (const btn of document.querySelectorAll(".info-btn")) {
        btn.onclick = (e) => { e.preventDefault(); const box = $(btn.dataset.info); box.hidden = !box.hidden; };
    }
    for (const el of wfInputs()) {
        const key = el.dataset.wf;
        const evt = el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input";
        el.addEventListener(evt, () => {
            if (!$("preset-details").hidden) renderPresetDetails();
            onFormChange(key);
        });
    }
    $("wf-select").onchange = onWorkflowSelect;
    $("wf-save-btn").onclick = saveWorkflowChanges;
    $("wf-saveas-btn").onclick = saveWorkflowAs;
    $("wf-rename-btn").onclick = renameWorkflow;
    $("wf-delete-btn").onclick = deleteWorkflow;
    $("lib-select-all").onclick = (e) => { e.preventDefault(); toggleSelectAll(); };
    $("show-full-path").onchange = renderPreview;
    $("add-to-queue").onclick = addToQueue;
    $("lib-path-form").onsubmit = (e) => { e.preventDefault(); navigate($("lib-path").value.trim()); };
    $("lib-add-root").onclick = () => saveRoots([...lib.roots, lib.dir]);
    $("lib-remove-root").onclick = () => saveRoots(lib.roots.filter(r => normPath(r) !== normPath(lib.dir)));
    $("es-save").onclick = saveEditSettings;
    $("es-revert").onclick = revertEditSettings;
    $("es-reset").onclick = () => { lib.esDraft = {}; renderEditSettings(); };
    $("es-cancel").onclick = exitEditMode;
    $("es-add-wf").onclick = () => {
        const name = $("es-new-name").value.trim();
        if (!name) { alert("Enter a workflow name."); return; }
        const base = lib.settings.workflows[name] || lib.settings.workflows.Custom;
        lib.esDraft.workflows = { ...(lib.esDraft.workflows || {}), [name]: workflowDiff(getFormWorkflow(), base) };
        $("es-new-name").value = "";
        renderEditSettings();
    };

    await presetsReady;  // content-type <select> options must exist before a workflow is applied
    lib.roots = (await api("/api/library/roots")).roots;
    let start = null;
    try { start = localStorage.getItem("libraryDir"); } catch (e) { /* ignore */ }
    await navigate(start || lib.roots[0]);
    if (!lib.dir) await navigate(lib.roots[0]);
}

initLibrary();
