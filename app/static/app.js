const STAGES = ["queued", "staged", "probed", "deinterlaced", "denoised", "dehaloed", "upscaled", "finalized"];

const TRASH_ICON = `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>`;

let browsePath = null;
let selected = new Set();
let presets = null;
let jobsById = {};
let lastRenderedJobs = [];
let openDetailId = null;
let logsTimer = null;

// Row-number editing (click the "#" label on an incomplete job's row) --
// module-level so it survives the full-tbody rebuild that both the SSE
// stream and manual refreshes do every render.
let editingRowId = null;
let editingDraftValue = "";

async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
        const body = await res.text();
        throw new Error(`${res.status}: ${body}`);
    }
    return res.status === 204 ? null : res.json();
}

// ----------------------------------------------------------------- browsing

async function loadBrowse(path) {
    const data = await api("/api/browse" + (path ? `?path=${encodeURIComponent(path)}` : ""));
    browsePath = data.path;
    document.getElementById("browse-path").textContent = data.path;
    const list = document.getElementById("browse-list");
    list.innerHTML = "";

    if (data.parent) {
        const up = document.createElement("div");
        up.className = "browse-entry dir";
        up.textContent = ".. (up)";
        up.onclick = () => loadBrowse(data.parent);
        list.appendChild(up);
    }

    for (const entry of data.entries) {
        const row = document.createElement("div");
        row.className = "browse-entry" + (entry.is_dir ? " dir" : "");
        if (entry.is_dir) {
            row.textContent = "\u{1F4C1} " + entry.name;
            row.onclick = () => loadBrowse(entry.path);
        } else {
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.checked = selected.has(entry.path);
            cb.onclick = (e) => {
                e.stopPropagation();
                toggleSelect(entry.path);
            };
            const label = document.createElement("span");
            label.textContent = entry.name;
            row.appendChild(cb);
            row.appendChild(label);
            row.onclick = () => toggleSelect(entry.path);
        }
        list.appendChild(row);
    }
}

function toggleSelect(path) {
    if (selected.has(path)) selected.delete(path); else selected.add(path);
    document.getElementById("selected-count").textContent =
        selected.size ? `${selected.size} file(s) selected` : "none selected";
    loadBrowse(browsePath);
}

// ------------------------------------------------------------------ presets

async function loadPresets() {
    presets = await api("/api/presets");
    const typeSel = document.getElementById("content-type");
    typeSel.innerHTML = "";
    for (const [key, type] of Object.entries(presets.content_types.types)) {
        const opt = document.createElement("option");
        opt.value = key; opt.textContent = type.label;
        if (key === presets.content_types.default) opt.selected = true;
        typeSel.appendChild(opt);
    }
    renderPresetDetails();
}

function renderPresetDetails() {
    const key = document.getElementById("content-type").value;
    const type = presets.content_types.types[key];
    const preset = presets.topaz_presets[type.topaz_preset];
    const d = presets.topaz_preset_display[type.topaz_preset];
    const tune = presets.denoise_tunes.tunes[type.denoise_tune];

    const row = (label, value) => `<div class="stack-row"><span class="stack-label">${label}</span><span>${value}</span></div>`;

    let html = "";
    html += row("Model", escapeHtml(d.model_label));
    html += row("Target resolution", escapeHtml(d.target_tier));
    html += `<div class="stack-row"><span class="stack-label">Upscale strategy</span></div>`;
    html += `<div class="stack-note">${escapeHtml(d.upscale_strategy)}</div>`;
    html += row("Pre-clean (Nyx)", d.precleanup_enabled ? escapeHtml(d.precleanup_label) : "disabled");
    if (d.resize_flags) {
        html += row("Resize filter", `${escapeHtml(d.resize_flags)} <span class="stack-note-inline">(avoids ringing/haloing on hard edges)</span>`);
    }
    html += row("Denoise tune", escapeHtml(tune.label));
    html += row("Encoder", escapeHtml(d.encoder_label));
    html += row("Container", escapeHtml(d.container));
    html += row("Audio", escapeHtml(d.audio_mode));

    document.getElementById("preset-details").innerHTML = html;
}

function togglePresetDetails() {
    const panel = document.getElementById("preset-details");
    const btn = document.getElementById("preset-toggle");
    panel.hidden = !panel.hidden;
    btn.innerHTML = panel.hidden ? "&#9654;" : "&#9660;";
    if (!panel.hidden) renderPresetDetails();
}

// Parses "HH:MM:SS", "MM:SS", or a bare seconds value into total seconds.
// Returns null for blank input, throws for anything else unparseable.
function parseTimecode(text) {
    text = (text || "").trim();
    if (!text) return null;
    const parts = text.split(":").map(p => p.trim());
    if (parts.some(p => p === "" || isNaN(Number(p)))) {
        throw new Error(`Invalid timecode: "${text}"`);
    }
    const nums = parts.map(Number);
    if (nums.length === 1) return nums[0];
    if (nums.length === 2) return nums[0] * 60 + nums[1];
    if (nums.length === 3) return nums[0] * 3600 + nums[1] * 60 + nums[2];
    throw new Error(`Invalid timecode: "${text}"`);
}

function getCropRange() {
    if (!document.getElementById("test-crop-enabled").checked) return { start: null, end: null };
    const start = parseTimecode(document.getElementById("test-crop-start").value);
    const end = parseTimecode(document.getElementById("test-crop-end").value);
    if (start === null || end === null) throw new Error("Enter both an In and an Out point for the test crop.");
    if (end <= start) throw new Error("Test crop Out point must be after the In point.");
    return { start, end };
}

async function submitJobs() {
    if (selected.size === 0) { alert("Select at least one file first."); return; }
    let crop;
    try {
        crop = getCropRange();
    } catch (e) {
        alert(e.message);
        return;
    }
    try {
        await api("/api/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                paths: Array.from(selected),
                deinterlace_enabled: document.getElementById("deinterlace-enabled").checked,
                denoise_enabled: document.getElementById("denoise-enabled").checked,
                dehalo_enabled: document.getElementById("dehalo-enabled").checked,
                content_type: document.getElementById("content-type").value,
                // The API's "skip_upscale" field predates the toggle stack and is
                // kept internally (server/db) to avoid a schema rename -- the UI
                // now shows its inverse as an "Upscale" toggle.
                skip_upscale: !document.getElementById("upscale-enabled").checked,
                crop_start_seconds: crop.start,
                crop_end_seconds: crop.end,
            }),
        });
    } catch (e) {
        alert("Couldn't add to queue: " + e.message);
        return;
    }
    selected.clear();
    document.getElementById("selected-count").textContent = "none selected";
    loadBrowse(browsePath);
}

function initTestCropToggle() {
    const cb = document.getElementById("test-crop-enabled");
    const inputs = document.getElementById("test-crop-inputs");
    cb.onchange = () => { inputs.hidden = !cb.checked; };
}

// Each toggle in the processing stack remembers its last state across
// sessions (per-viewer convenience, not something the server needs to
// track) -- id -> [localStorage key, default checked state].
const TOGGLE_STACK = {
    "deinterlace-enabled": ["deinterlaceEnabled", true],
    "denoise-enabled": ["denoiseEnabled", false],
    "dehalo-enabled": ["dehaloEnabled", false],
    "upscale-enabled": ["upscaleEnabled", true],
};

function initToggleStack() {
    for (const [id, [key, defaultChecked]] of Object.entries(TOGGLE_STACK)) {
        const cb = document.getElementById(id);
        try {
            const stored = localStorage.getItem(key);
            cb.checked = stored === null ? defaultChecked : stored === "true";
        } catch (e) { cb.checked = defaultChecked; /* private browsing etc */ }
        cb.onchange = () => {
            try { localStorage.setItem(key, cb.checked); } catch (e) { /* ignore */ }
        };
    }
}

// --------------------------------------------------------------- job table

// job.stage is the LAST COMPLETED stage (see db.py/worker.py's
// STAGE_RUNNERS: it maps that value to the function that runs NEXT) -- so
// while status is "running", the stage actually executing is the one AFTER
// job.stage in STAGES, not job.stage itself. Getting this backwards makes
// the dashboard look like it's running the wrong toggle entirely (e.g.
// stage="denoised" while dehalo.py is the one actually mid-run).
function runningStageIndex(job) {
    const lastDoneIdx = STAGES.indexOf(job.stage);
    return Math.min(lastDoneIdx + 1, STAGES.length - 1);
}

// Which enabled/toggle flag governs whether STAGES[i] actually ran anything,
// or was completed as an instant no-op passthrough (see each stages/*.py's
// own "disabled -- passing through unchanged" branch).
function isStageSkipped(job, i) {
    switch (STAGES[i]) {
        case "deinterlaced": return !job.deinterlace_enabled;
        case "denoised": return !job.denoise_enabled;
        case "dehaloed": return !job.dehalo_enabled;
        case "upscaled": return !!job.skip_upscale;
        default: return false;
    }
}

function stageTrack(job) {
    const lastDoneIdx = STAGES.indexOf(job.stage);
    const runningIdx = runningStageIndex(job);
    const isDone = job.status === "done";
    return STAGES.map((s, i) => {
        if (!isDone && i === runningIdx && job.status === "running") {
            const pct = Math.max(0, Math.min(100, job.progress_percent ?? 0));
            return `<span class="stage-dot current" title="${s}: ${pct.toFixed(0)}%">
                <span class="stage-dot-fill" style="width:${pct}%"></span>
                <span class="stage-dot-label">${pct.toFixed(0)}%</span>
            </span>`;
        }
        let cls = "stage-dot";
        if (i <= lastDoneIdx || isDone) cls += " done";
        return `<span class="${cls}" title="${s}"></span>`;
    }).join("");
}

function statusLabel(job) {
    if (job.status === "failed" && job.failure_category === "oom") return "FAILED (OOM)";
    if (job.status === "failed" && job.failure_category === "disk_full") return "FAILED (Disk Full)";
    if (job.status === "needs_restart") return "NEEDS RESTART (VRAM)";
    return job.status.replace(/_/g, " ");
}

// A checkpointed generative job's settings carry checkpoint_completed_segments
// while it's mid-flight (cleared again once the job finishes successfully) --
// used to show ratcheted progress instead of a job looking reset after a
// crash/pause/resume cycle.
function checkpointSummary(job) {
    const segs = job.settings && job.settings.checkpoint_completed_segments;
    if (!segs || !segs.length) return null;
    const resumeFrom = segs[segs.length - 1].end;
    const mins = Math.floor(resumeFrom / 60);
    const secs = Math.floor(resumeFrom % 60);
    return `${segs.length} checkpoint segment(s) complete, resuming from ${mins}:${String(secs).padStart(2, "0")}`;
}

function settingsSummary(job) {
    const s = job.settings || {};
    const bits = [];
    if (s.scan_type) bits.push(s.scan_type);
    if (typeof s.confidence === "number") bits.push(`conf ${s.confidence.toFixed(2)}`);
    if (s.width && s.height) bits.push(`${s.width}x${s.height}`);
    return bits.join(" · ");
}

// Row-number cell: completed jobs (always pinned to the top, oldest
// completion first -- see db.list_jobs_ordered) just show a plain number.
// Every other job's "#" label can be clicked into a number input, plus
// to-top/up/down/to-bottom move buttons -- both drive POST
// /api/jobs/{id}/reorder (see moveJob/commitRowNum below).
function rowNumberCellHtml(job, i, jobs, completedCount) {
    const rank = i + 1;
    if (job.status === "done") {
        return `<td class="row-num"><div class="row-num-main"><span class="row-num-label">${rank}</span></div></td>`;
    }
    const hasIncompleteAbove = i > completedCount;
    const hasBelow = i < jobs.length - 1;
    const isEditing = job.id === editingRowId;

    const numHtml = isEditing
        ? `<input type="number" class="row-num-input" min="1" max="${jobs.length}" step="1" value="${escapeHtml(editingDraftValue)}">
           <button class="row-num-save" title="Save">&#9989;</button>
           <button class="row-num-cancel" title="Cancel">&#10060;</button>`
        : `<span class="row-num-label" data-editable="true" title="Click to move to a specific row">${rank}</span>`;

    return `<td class="row-num">
        <div class="row-num-main">${numHtml}</div>
        <div class="row-num-moves">
            <button class="row-move" data-dir="top" title="Move to top" ${hasIncompleteAbove ? "" : "disabled"}>&#9195;</button>
            <button class="row-move" data-dir="up" title="Move up" ${hasIncompleteAbove ? "" : "disabled"}>&#128316;</button>
            <button class="row-move" data-dir="down" title="Move down" ${hasBelow ? "" : "disabled"}>&#128317;</button>
            <button class="row-move" data-dir="bottom" title="Move to bottom" ${hasBelow ? "" : "disabled"}>&#9196;</button>
        </div>
    </td>`;
}

function renderJobs(jobs) {
    jobsById = Object.fromEntries(jobs.map(j => [j.id, j]));
    lastRenderedJobs = jobs;
    const tbody = document.getElementById("job-rows");
    tbody.innerHTML = "";
    document.getElementById("empty-msg").style.display = jobs.length ? "none" : "block";

    const completedCount = jobs.filter(j => j.status === "done").length;

    jobs.forEach((job, i) => {
        const tr = document.createElement("tr");
        tr.className = "job-row" + (job.id === openDetailId ? " selected" : "");
        tr.onclick = () => openDetail(job.id);
        const displayName = (job.settings && job.settings.display_filename) || job.original_filename;
        const isRunning = job.status === "running";
        tr.innerHTML = `
            ${rowNumberCellHtml(job, i, jobs, completedCount)}
            <td class="filename" title="${displayName}">${displayName}</td>
            <td><span class="badge ${job.status}">${statusLabel(job)}</span></td>
            <td><div class="stage-track">${stageTrack(job)}</div></td>
            <td style="color:var(--text-dim);font-size:12px">${settingsSummary(job)}</td>
            <td style="color:var(--text-dim);font-size:12px">${new Date(job.updated_at * 1000).toLocaleString()}</td>
            <td>
                <button class="small delete-btn" title="${isRunning ? "Abort and delete this job" : "Delete this record"}">${TRASH_ICON}</button>
            </td>
        `;
        tr.querySelector(".delete-btn").onclick = (e) => {
            e.stopPropagation();
            deleteJob(job.id);
        };

        const rowNumLabel = tr.querySelector(".row-num-label[data-editable]");
        if (rowNumLabel) {
            rowNumLabel.onclick = (e) => {
                e.stopPropagation();
                startEditRowNum(job.id, i + 1);
            };
        }
        const saveBtn = tr.querySelector(".row-num-save");
        if (saveBtn) saveBtn.onclick = (e) => { e.stopPropagation(); commitRowNum(job.id); };
        const cancelBtn = tr.querySelector(".row-num-cancel");
        if (cancelBtn) cancelBtn.onclick = (e) => { e.stopPropagation(); cancelEditRowNum(); };
        const input = tr.querySelector(".row-num-input");
        if (input) {
            input.onclick = (e) => e.stopPropagation();
            input.oninput = () => { editingDraftValue = input.value; };
            input.onkeydown = (e) => {
                e.stopPropagation();
                if (e.key === "Enter") commitRowNum(job.id);
                if (e.key === "Escape") cancelEditRowNum();
            };
        }
        tr.querySelectorAll(".row-move").forEach(btn => {
            btn.onclick = (e) => {
                e.stopPropagation();
                if (btn.disabled) return;
                moveJob(job.id, btn.dataset.dir);
            };
        });

        tbody.appendChild(tr);
    });

    if (editingRowId) focusRowNumInput();
}

function startEditRowNum(jobId, currentRank) {
    editingRowId = jobId;
    editingDraftValue = String(currentRank);
    renderJobs(lastRenderedJobs);
}

function cancelEditRowNum() {
    editingRowId = null;
    editingDraftValue = "";
    renderJobs(lastRenderedJobs);
}

async function commitRowNum(jobId) {
    const n = parseInt(editingDraftValue, 10);
    if (!Number.isFinite(n) || n < 1) {
        alert("Enter a valid row number.");
        return;
    }
    editingRowId = null;
    editingDraftValue = "";
    try {
        await api(`/api/jobs/${jobId}/reorder`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ row: n }),
        });
    } catch (e) {
        alert("Couldn't reorder: " + e.message);
    }
    refreshJobsNow();
}

async function moveJob(jobId, direction) {
    try {
        await api(`/api/jobs/${jobId}/reorder`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ direction }),
        });
    } catch (e) {
        alert("Couldn't reorder: " + e.message);
        return;
    }
    refreshJobsNow();
}

async function refreshJobsNow() {
    try {
        renderJobs(await api("/api/jobs"));
    } catch (e) { /* the live stream will catch up shortly regardless */ }
}

function focusRowNumInput() {
    const input = document.querySelector(".row-num-input");
    if (input) {
        input.focus();
        input.select();
    }
}

// -------------------------------------------------------------- job detail

async function deleteJob(jobId) {
    const job = jobsById[jobId];
    const name = (job && job.settings && job.settings.display_filename) || (job && job.original_filename) || jobId;
    const isRunning = job && job.status === "running";
    const prompt = isRunning
        ? `This job is currently running -- abort it and remove it from the queue?\n\n${name}\n\n(Any partial/staged files for it are cleaned up. No finished output exists yet.)`
        : `Remove this record from the queue?\n\n${name}\n\n(This only clears the dashboard entry -- any output file already on disk is untouched.)`;
    if (!confirm(prompt)) return;
    try {
        await api(`/api/jobs/${jobId}`, { method: "DELETE" });
    } catch (e) {
        alert("Couldn't delete: " + e.message);
        return;
    }
    if (openDetailId === jobId) closeDetail();
}

async function deleteDoneJobs() {
    if (!confirm("Remove every finished job from the queue list?\n\n(This only clears dashboard entries -- output files already on disk are untouched.)")) return;
    try {
        const res = await api("/api/jobs/done", { method: "DELETE" });
        if (openDetailId && jobsById[openDetailId] && jobsById[openDetailId].status === "done") closeDetail();
        if (!res.deleted) alert("No done jobs to remove.");
    } catch (e) {
        alert("Couldn't delete done jobs: " + e.message);
    }
}

async function rerunJob(jobId) {
    try {
        await api(`/api/jobs/${jobId}/rerun`, { method: "POST" });
    } catch (e) {
        alert("Couldn't queue a rerun: " + e.message);
        return;
    }
    refreshDetail();
}

async function abortJob(jobId) {
    const job = jobsById[jobId];
    const name = (job && job.settings && job.settings.display_filename) || (job && job.original_filename) || jobId;
    if (!confirm(`Abort this job?\n\n${name}`)) return;
    try {
        await api(`/api/jobs/${jobId}/abort`, { method: "POST" });
    } catch (e) {
        alert("Couldn't abort: " + e.message);
        return;
    }
    refreshDetail();
}

async function openDetail(jobId) {
    openDetailId = jobId;
    document.getElementById("detail-resume").hidden = true;
    document.getElementById("detail").classList.add("open");
    await refreshDetail();
    clearInterval(logsTimer);
    logsTimer = setInterval(refreshDetail, 2000);
}

function closeDetail() {
    openDetailId = null;
    document.getElementById("detail").classList.remove("open");
    clearInterval(logsTimer);
}

const retryingJobs = new Set();

async function refreshDetail() {
    if (!openDetailId) return;
    let job;
    try {
        job = await api(`/api/jobs/${openDetailId}`);
    } catch (e) {
        return;
    }
    if (job.id !== openDetailId) return;
    const displayName = (job.settings && job.settings.display_filename) || job.original_filename;
    document.getElementById("detail-title").textContent = displayName;
    document.getElementById("detail-id").textContent = `${job.id} · ${job.original_nas_path}`;

    const abortBtn = document.getElementById("detail-abort");
    const canAbort = job.status === "running" || job.status === "pending";
    abortBtn.disabled = !canAbort;
    abortBtn.onclick = canAbort ? () => abortJob(job.id) : null;
    document.getElementById("detail-rerun").onclick = () => rerunJob(job.id);
    document.getElementById("detail-delete").onclick = () => deleteJob(job.id);
    const canRetry = job.status === "failed" || job.status === "needs_restart";
    document.getElementById("detail-resume").hidden = !canRetry;
    const retryBtn = document.getElementById("detail-retry");
    retryBtn.disabled = retryingJobs.has(job.id);
    retryBtn.textContent = retryingJobs.has(job.id) ? "Queuing…" : "Retry / continue from checkpoint";
    retryBtn.onclick = canRetry ? () => retryJob(job.id) : null;

    let html = "";

    const outputName = job.current_file ? job.current_file.split(/[\\/]/).pop() : null;
    html += `<div class="stack-row"><span class="stack-label">Output file name</span></div>`;
    html += `<div class="stack-note" style="margin-bottom:12px">${outputName ? escapeHtml(outputName) : "(not yet produced)"}</div>`;

    // Only the stages actually selected for this job are shown -- a job's
    // toggled-off stages (deinterlace/denoise/dehalo/upscale) are permanently
    // no-op passthroughs, so listing them just as "(skipped)" clutters the
    // one place meant to give an at-a-glance read of how much work (and
    // roughly how long) a job actually involves.
    html += `<div class="stage-list">` + STAGES.map((s, i) => ({ s, i }))
        .filter(({ i }) => !isStageSkipped(job, i))
        .map(({ s, i }) => {
        const lastDoneIdx = STAGES.indexOf(job.stage);
        const runningIdx = runningStageIndex(job);
        const isDone = job.status === "done" || i <= lastDoneIdx;
        const isCurrent = i === runningIdx && job.status === "running";
        const cls = isDone ? "done" : (isCurrent ? "current" : "pending");
        const dotCls = isDone ? "done" : (isCurrent ? "current" : "");
        let label;
        if (isCurrent) {
            const pct = Math.max(0, Math.min(100, job.progress_percent ?? 0));
            label = `${s} (running ${pct.toFixed(0)}%)`;
        } else if (isDone) {
            label = `${s} (done 100%)`;
        } else {
            label = s;
        }
        return `<div class="stage-list-row ${cls}"><span class="stage-dot ${dotCls}"></span>${escapeHtml(label)}</div>`;
    }).join("") + `</div>`;

    const checkpointNote = checkpointSummary(job);
    if (checkpointNote) {
        html += `<div class="stack-note" style="margin-top:8px">${escapeHtml(checkpointNote)}</div>`;
    }

    if ((job.status === "failed" || job.status === "needs_restart") && job.error_message) {
        html += `<div class="error-box">${escapeHtml(job.error_message)}</div>`;
    }

    if (job.status === "needs_review") {
        const s = job.settings || {};
        html += `<div class="review-box">
            <div><strong>Needs review</strong> -- ${escapeHtml(job.error_message || "low-confidence detection")}</div>
            <div style="font-size:12px;color:var(--text-dim);margin-top:6px">
                detected: scan_type=${s.scan_type ?? "?"} tff=${s.tff ?? "?"} confidence=${s.confidence?.toFixed?.(2) ?? "?"}
            </div>
            <div class="row">
                <select id="review-scan-type">
                    <option value="progressive" ${s.scan_type === "progressive" ? "selected" : ""}>progressive</option>
                    <option value="interlaced" ${s.scan_type === "interlaced" ? "selected" : ""}>interlaced (QTGMC Bob)</option>
                    <option value="telecine" ${s.scan_type === "telecine" ? "selected" : ""}>telecine (VIVTC IVTC)</option>
                </select>
                <select id="review-tff">
                    <option value="true" ${s.tff !== false ? "selected" : ""}>TFF</option>
                    <option value="false" ${s.tff === false ? "selected" : ""}>BFF</option>
                </select>
            </div>
            <div class="row">
                <button class="small primary" onclick="confirmReview('${job.id}')">Confirm & continue</button>
            </div>
        </div>`;
    }

    html += `<div class="kv">${escapeHtml(JSON.stringify(job.settings, null, 2))}</div>`;
    html += `<div class="logs" id="detail-logs">${job.logs.map(l =>
        `[${l.stage}] ${escapeHtml(l.message)}`).join("\n")}</div>`;

    document.getElementById("detail-body").innerHTML = html;
    const logsEl = document.getElementById("detail-logs");
    if (logsEl) logsEl.scrollTop = logsEl.scrollHeight;
}

async function confirmReview(jobId) {
    const scanType = document.getElementById("review-scan-type").value;
    const tff = document.getElementById("review-tff").value === "true";
    await api(`/api/jobs/${jobId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scan_type: scanType, tff, proceed: true }),
    });
    refreshDetail();
}

async function retryJob(jobId) {
    if (retryingJobs.has(jobId)) return;
    retryingJobs.add(jobId);
    const retryBtn = document.getElementById("detail-retry");
    retryBtn.disabled = true;
    retryBtn.textContent = "Queuing…";
    try {
        await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
        await refreshJobsNow();
    } catch (e) {
        alert("Couldn't retry this job: " + e.message);
    } finally {
        retryingJobs.delete(jobId);
        retryBtn.disabled = false;
        retryBtn.textContent = "Retry / continue from checkpoint";
        await refreshDetail();
    }
}

function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
}

// -------------------------------------------------------------------- live

function renderPauseControls(pauseState) {
    const pauseBtn = document.getElementById("pause-btn");
    const resumeBtn = document.getElementById("resume-btn");
    if (pauseState === "paused") {
        pauseBtn.textContent = "Paused";
        pauseBtn.disabled = true;
    } else if (pauseState === "pausing") {
        pauseBtn.textContent = "Pausing at next checkpoint...";
        pauseBtn.disabled = true;
    } else {
        pauseBtn.textContent = "Pause at next checkpoint";
        pauseBtn.disabled = false;
    }
    resumeBtn.disabled = pauseState === "running";
}

async function pauseWorker() {
    renderPauseControls("pausing");  // immediate feedback, SSE will confirm shortly
    try {
        await api("/api/worker/pause", { method: "POST" });
    } catch (e) {
        alert("Couldn't request pause: " + e.message);
    }
}

async function resumeWorker() {
    try {
        await api("/api/worker/resume", { method: "POST" });
    } catch (e) {
        alert("Couldn't resume: " + e.message);
        return;
    }
    renderPauseControls("running");
}

function connectStream() {
    const es = new EventSource("/api/stream");
    es.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        renderJobs(data.jobs);
        document.getElementById("worker-status").textContent =
            data.current_job_id ? `worker: running ${data.current_job_id}` : "worker: idle";
        renderPauseControls(data.worker_pause_state || "running");
    };
    es.onerror = () => {
        es.close();
        setTimeout(connectStream, 2000);
    };
}

// -------------------------------------------------------------------- init

document.getElementById("submit-btn").onclick = submitJobs;
document.getElementById("pause-btn").onclick = pauseWorker;
document.getElementById("resume-btn").onclick = resumeWorker;
document.getElementById("delete-done-btn").onclick = deleteDoneJobs;
document.getElementById("detail-close").onclick = closeDetail;
document.getElementById("preset-toggle").onclick = togglePresetDetails;
document.getElementById("content-type").onchange = () => {
    if (!document.getElementById("preset-details").hidden) renderPresetDetails();
};

initToggleStack();
initTestCropToggle();
loadPresets();
loadBrowse(null);
connectStream();
