const STAGES = ["queued", "staged", "probed", "deinterlaced", "denoised", "upscaled", "finalized"];

let browsePath = null;
let selected = new Set();
let presets = null;
let jobsById = {};
let openDetailId = null;
let logsTimer = null;

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
    html += row("Resize filter", `${escapeHtml(d.resize_flags)} <span class="stack-note-inline">(avoids ringing/haloing on hard edges)</span>`);
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

async function submitJobs() {
    if (selected.size === 0) { alert("Select at least one file first."); return; }
    await api("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            paths: Array.from(selected),
            denoise_enabled: document.getElementById("denoise-enabled").checked,
            content_type: document.getElementById("content-type").value,
            skip_upscale: document.getElementById("skip-upscale").checked,
        }),
    });
    selected.clear();
    document.getElementById("selected-count").textContent = "none selected";
    loadBrowse(browsePath);
}

// "Skip Upscaling" remembers its last state across sessions (per-viewer
// convenience, not something the server needs to track).
function initSkipUpscaleCheckbox() {
    const cb = document.getElementById("skip-upscale");
    try {
        cb.checked = localStorage.getItem("skipUpscale") === "true";
    } catch (e) { /* private browsing etc -- default unchecked */ }
    cb.onchange = () => {
        try { localStorage.setItem("skipUpscale", cb.checked); } catch (e) { /* ignore */ }
    };
}

// --------------------------------------------------------------- job table

function stageTrack(job) {
    const curIdx = STAGES.indexOf(job.stage);
    const isDone = job.status === "done";
    return STAGES.map((s, i) => {
        let cls = "stage-dot";
        if (i < curIdx || isDone) cls += " done";
        else if (i === curIdx && job.status === "running") cls += " current";
        return `<span class="${cls}" title="${s}"></span>`;
    }).join("");
}

function settingsSummary(job) {
    const s = job.settings || {};
    const bits = [];
    if (s.scan_type) bits.push(s.scan_type);
    if (typeof s.confidence === "number") bits.push(`conf ${s.confidence.toFixed(2)}`);
    if (s.width && s.height) bits.push(`${s.width}x${s.height}`);
    return bits.join(" · ");
}

function renderJobs(jobs) {
    jobsById = Object.fromEntries(jobs.map(j => [j.id, j]));
    const tbody = document.getElementById("job-rows");
    tbody.innerHTML = "";
    document.getElementById("empty-msg").style.display = jobs.length ? "none" : "block";

    for (const job of [...jobs].reverse()) {
        const tr = document.createElement("tr");
        tr.className = "job-row" + (job.id === openDetailId ? " selected" : "");
        tr.onclick = () => openDetail(job.id);
        const displayName = (job.settings && job.settings.display_filename) || job.original_filename;
        const canDelete = job.status !== "running";
        tr.innerHTML = `
            <td class="filename" title="${displayName}">${displayName}</td>
            <td><span class="badge ${job.status}">${job.status}</span></td>
            <td><div class="stage-track">${stageTrack(job)}</div></td>
            <td style="color:var(--text-dim);font-size:12px">${settingsSummary(job)}</td>
            <td style="color:var(--text-dim);font-size:12px">${new Date(job.updated_at * 1000).toLocaleString()}</td>
            <td>
                <button class="small delete-btn" title="${canDelete ? "Delete this record" : "Can't delete a running job"}"
                    ${canDelete ? "" : "disabled"}>&times;</button>
            </td>
        `;
        tr.querySelector(".delete-btn").onclick = (e) => {
            e.stopPropagation();
            deleteJob(job.id);
        };
        tbody.appendChild(tr);
    }
}

// -------------------------------------------------------------- job detail

async function deleteJob(jobId) {
    const job = jobsById[jobId];
    const name = (job && job.settings && job.settings.display_filename) || (job && job.original_filename) || jobId;
    if (!confirm(`Remove this record from the queue?\n\n${name}\n\n(This only clears the dashboard entry -- any output file already on disk is untouched.)`)) {
        return;
    }
    try {
        await api(`/api/jobs/${jobId}`, { method: "DELETE" });
    } catch (e) {
        alert("Couldn't delete: " + e.message);
        return;
    }
    if (openDetailId === jobId) closeDetail();
}

async function openDetail(jobId) {
    openDetailId = jobId;
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

async function refreshDetail() {
    if (!openDetailId) return;
    let job;
    try {
        job = await api(`/api/jobs/${openDetailId}`);
    } catch (e) {
        return;
    }
    const displayName = (job.settings && job.settings.display_filename) || job.original_filename;
    document.getElementById("detail-title").textContent = displayName;
    document.getElementById("detail-id").textContent = `${job.id} · ${job.original_nas_path}`;

    let html = "";

    if (job.status === "failed" && job.error_message) {
        html += `<div class="error-box">${escapeHtml(job.error_message)}</div>`;
        html += `<button class="small" onclick="retryJob('${job.id}')">Retry stage</button>`;
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
    await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
    refreshDetail();
}

function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
}

// -------------------------------------------------------------------- live

function connectStream() {
    const es = new EventSource("/api/stream");
    es.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        renderJobs(data.jobs);
        document.getElementById("worker-status").textContent =
            data.current_job_id ? `worker: running ${data.current_job_id}` : "worker: idle";
    };
    es.onerror = () => {
        es.close();
        setTimeout(connectStream, 2000);
    };
}

// -------------------------------------------------------------------- init

document.getElementById("submit-btn").onclick = submitJobs;
document.getElementById("detail-close").onclick = closeDetail;
document.getElementById("preset-toggle").onclick = togglePresetDetails;
document.getElementById("content-type").onchange = () => {
    if (!document.getElementById("preset-details").hidden) renderPresetDetails();
};

initSkipUpscaleCheckbox();
loadPresets();
loadBrowse(null);
connectStream();
