# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local pipeline tool that automates AI upscaling of an SD/interlaced video library, following the
non-generative-AI-upscaling workflow of deinterlace/IVTC (Hybrid's QTGMC/VIVTC via VapourSynth) →
optional denoise (HandBrake NLMeans) → AI upscale (Topaz Video AI, driven headlessly via its bundled
ffmpeg). A FastAPI backend with a background worker thread processes a SQLite-backed job queue; a
plain-JS single page (no build step) is the dashboard, updated live via SSE.

This is tightly coupled to tool installs on one specific Windows machine (Topaz Video AI, Hybrid,
HandBrake, MakeMKV) — see "Machine-specific configuration" below before assuming anything here is
portable.

## Commands

```bash
pip install -r requirements.txt
python -m app.server          # starts the server at http://127.0.0.1:8756
start_pipeline.bat            # same, plus opens the dashboard in Brave
```

There is no build step, linter, or automated test suite. Verify changes by starting the server and
submitting a short clip through the dashboard, then watching that job's logs (or `pipeline.db`'s
`job_logs` table directly) for the stage you touched. `python -m py_compile app/*.py app/stages/*.py`
catches syntax errors cheaply before a real run.

Kill a running dev server with `Get-Process python | Stop-Process -Force` (PowerShell) before
restarting — there's no reload/watch mode.

## Architecture

### Job state machine (app/db.py, app/worker.py)

A job's `stage` column is the *last completed* stage; `worker.STAGE_RUNNERS` maps that value to the
function that runs *next* (e.g. `stage='probed'` dispatches to `deinterlace.run`). Each stage function
only calls `db.update_job(..., stage=<itself>, status='pending')` after confirming its output file
exists — so a mid-stage crash leaves `stage` unchanged and `status='running'`; `recover_running_jobs()`
(called on startup) resets those back to `pending`, which naturally redoes just that one stage. Don't
break this invariant when adding a stage: write the output, verify it, *then* advance `stage`.

`db.py` uses a single process-wide SQLite connection behind a lock rather than one connection per call
— high-frequency `db.log()` calls (every subprocess output line) caused real "attempt to write a
readonly database" failures under Windows WAL-mode connection churn.

### Stage pipeline (app/stages/)

`ingest → probe → deinterlace → denoise → dehalo → upscale → finalize`. Each stage reads/writes the
job's `settings_json` blob (`db.get_settings`/`db.merge_settings`) to pass detected/computed values
forward (scan type, crop, PAR, per-stage summaries for the final metadata embed, etc.) rather than
re-deriving them.

- **probe**: `ffmpeg -vf idet` sampled at a few points classifies progressive vs. interlaced. When
  idet is ambiguous (the classic telecine signature — still sees combing since it's literally 60
  fields/sec), a real VIVTC `VFM` dry-run (via the portable VapourSynth Python, not through ffmpeg)
  checks how much combing survives field-matching to distinguish telecine from true interlace. Below
  `config.json`'s `probe.confidence_threshold`, the job goes to `needs_review` instead of guessing.
- **deinterlace**: renders a `.vpy` from a Jinja template (`app/vpy_templates/`) and drives it with
  `VSPipe.exe` piped into ffmpeg. **Reuses Hybrid's own bundled, fully portable VapourSynth runtime**
  (own `python.exe`, `VSPipe.exe`, QTGMC script, plugin DLLs — see `config.json`'s `tools.vspipe` /
  `vs_plugins`) instead of a separate VapourSynth install, so results match what Hybrid's GUI would
  produce. Plugin DLLs are loaded explicitly by path (`_plugins.vpy.j2`); a few (fft3dfilter, dfttest)
  need `vsfilters\Support` added to the DLL search path via `os.add_dll_directory` because their
  dependency DLLs live in a different folder than the plugin itself — Windows won't find them otherwise.
- **dehalo** (app/stages/dehalo.py): two chained `tvai_up` passes (Iris then Artemis, both at 1:1 —
  no resolution change) to remove ringing/halo artifacts from oversharpened DVD sources. The model
  shortnames/params (`app/presets/dehalo.json`) were reverse-engineered by capturing the real
  `ffmpeg.exe` command line Topaz Video AI itself ran for a given GUI enhancement stack, not read off
  the GUI's slider labels — "Recover detail" in particular does **not** show up as a literal `details`
  value in the real command; see the preset's own notes.
- **denoise** / **dehalo** / **upscale** are each independently toggled (`denoise_enabled`,
  `dehalo_enabled`, `skip_upscale` — the UI shows the last one inverted as an "Upscale" toggle) and
  early-return as a no-op passthrough when their own flag says not to run. They do **not** cascade off
  of each other — e.g. `skip_upscale` (Upscale off) no longer forces denoise/dehalo to also skip; that
  was the pre-toggle-stack behavior and broke once Dehalo became its own independent toggle sitting
  between Denoise and Upscale (confirmed by testing: Dehalo silently never ran whenever Upscale was
  off). `deinterlace_enabled` works the same way for the deinterlace stage.
- **upscale** (app/stages/upscale.py + app/topaz_models.py): drives Topaz's own bundled `ffmpeg.exe`
  and its `tvai_up` filter directly (confirmed via `ffmpeg -h filter=tvai_up`). Several non-obvious
  things had to be reverse-engineered here — see "Topaz ffmpeg gotchas" below. `topaz_models.py` is
  shared with `server.py` (for the UI's human-readable preset panel) specifically so the *actual*
  scale-chaining logic and the UI's *description* of it can't drift apart.
- **finalize**: the **only** place that decides the final filename tag and embeds the full
  processing-history metadata (previously split between deinterlace.py's `skip_upscale`-terminal branch
  and upscale.py — that split assumed one of those two was always "the last stage", which stopped being
  true once denoise/dehalo/upscale became independently toggleable). Always does one `-c copy` remux to
  attach `-metadata` tags and standardize on `.mkv` regardless of which stages actually ran, then moves
  the file to `original_nas_path`'s folder and demangles the name via `naming.py`. The original source
  file is never touched or deleted (it's archival).

### Filename tagging (app/naming.py)

Bracket tags (`[DVD]`, `[480i]`, ...) evolve through the pipeline, but **the terminal output (either
`set_upscaled_tag` or `set_final_progressive_tag`) drops every prior tag and keeps only the single
final one** (e.g. `Show S01E01 [DVD] [480i].mkv` → `Show S01E01 [Upscaled 1080p].mkv`) — deliberate,
not a bug; source-type/interim tags aren't meaningful once the deliverable exists.

### skip_upscale (the "Upscale" toggle)

A job-level flag that makes `upscale.py` no-op — nothing more. It used to also force `denoise`/`dehalo`
to no-op (an "Upscale off means deinterlace-only" mode), but that broke independent toggling once Dehalo
shipped as its own stage between Denoise and Upscale, so each of the four toggles now only controls its
own stage. `ingest.py` still short-circuits entirely (no copy, no processing at all — job jumps straight
to `finalized`) when `skip_upscale` is set AND `denoise_enabled`/`dehalo_enabled` are both off AND the
source filename already carries a progressive-resolution tag (e.g. `[1080p]`) — genuinely nothing to do
in that specific combination; don't widen this check without also checking those two flags.

### Topaz ffmpeg gotchas (all discovered by hitting them, not from docs)

- Topaz's bundled `ffmpeg.exe`/`ffprobe.exe` is built `--disable-decoder=h264 --disable-decoder=hevc`
  with no software fallback — only hardware decoders exist, and its auto-pick logic defaults to
  QSV, which fails hard on an NVIDIA-only machine. `decoder_util.py` forces the matching `*_cuvid`
  decoder explicitly based on the source's actual codec.
- `tvai_up`'s `w`/`h` params are **not** a real resize target — they're "estimate" hints only used
  when frame-sampling auto-estimation (`estimate=N`) is enabled. The real control is the integer
  `scale` param, and each model supports a different, restricted set of values (e.g. `gcg-5` is
  1/2/4, `ganim-1` is *only* 2) — read from that model's own JSON under
  `C:\ProgramData\Topaz Labs LLC\Topaz Video\models\<model>.json` (`topaz_models.available_scales`),
  never hardcoded. When no single available scale covers the requested tier, `build_scale_passes`
  chains the model's largest scale repeatedly (e.g. two 2x `ganim-1` passes for a ~4x target) rather
  than falling back to a plain resize for the shortfall — a real, requested quality improvement over
  the "one AI pass + resize the rest" approach. A final `scale=` filter always lands on the exact
  target dimensions afterward; it defaults to `bicubic` (not `lanczos`, which rings/haloes on hard
  edges — a real artifact on flat-color animation).
- `tvai_up`'s internal pipeline works in higher bit depth (rgb48le/etc.) — feed that straight into
  `h264_nvenc` and it errors ("10 bit encode not supported"); an explicit `format=yuv420p` after the
  filter chain is required.
- Running Topaz's `ffmpeg.exe` standalone (not launched by the Topaz GUI app) needs the `TVAI_MODEL_DIR`
  env var set explicitly (`config.json`'s `tvai_model_dir`) or `tvai_up` fails with "Model not found"
  even though the model files are right there on disk.
- **MP4 silently drops custom `-metadata key=value` tags** (its metadata model only recognizes a fixed
  vocabulary); Matroska doesn't have this restriction. This is *the* reason every terminal/delivered
  output uses `.mkv`, not just an aesthetic choice — an MP4 terminal output was shipped once and its
  embedded processing-history metadata silently vanished.

### Content types / presets (app/presets/)

The UI exposes a "Film (Non-Generative)" / "Film (Generative)" / "Animation" choice
(`content_types.json`); the server resolves that to a `(topaz_preset, denoise_tune)` pair before
creating the job (`server.py`'s `api_create_jobs`) — the frontend never needs to know individual
preset filenames. Presets (`topaz_film.json`, `topaz_animation.json`, `topaz_film_generative.json`)
are meant to be hand-tuned over time, not treated as fixed; `resize_flags`, `tvai_up_params`, and
encoder settings are all preset-level knobs.

A preset's `"engine"` field picks the upscale mechanism: absent/`"tvai_up"` (default) uses the
classic direct ffmpeg `tvai_up` filter path (`upscale.py`'s main `run()`); `"neuroserver"` routes to
`upscale.py`'s `_run_generative()` instead — see below, this is a completely different tool with its
own set of gotchas. `server.py`'s `_preset_display()` branches the same way for the UI's preset panel.

### Generative (Starlight) engine (app/stages/upscale.py's `_run_generative`)

Topaz's newer generative/diffusion models (marketed as "Starlight" — Starlight Sharp, Starlight Mini,
etc.) are **not reachable through ffmpeg's `tvai_up` filter at all** — confirmed by testing: none of
`astra`/`astrahq`/`astrafast`/`astrasharp`/`sls` appear in `tvai_up`'s compiled model enum on this
Topaz Video AI version (1.7.0.0), even though their model JSONs exist under `tvai_model_dir`. They're
served by a completely separate local process, `neuroserver.exe` (bundled at
`neuroserver\neuroserver.exe` next to Topaz's ffmpeg), discovered and reverse-engineered via a
Process Monitor capture of a real Topaz Video AI GUI render (Starlight Sharp specifically) since none
of this is documented anywhere:

- **Invocation**: `neuroserver.exe --once --input-path <file> --output-path <file> --input-width
  --input-height --output-width --output-height --upscale-factor --max-gpu-mem --filters
  '[{"model": "<bare name>"}]' --ffmpeg-encoding "<ffmpeg args>"`. The `model` value is the **bare**
  model name (e.g. `"astrasharp"`) — NOT the fully-qualified `"astrasharp-win-nvidia-1gpu"`-style key
  that appears in the filter's own "Model X not found, available models: [...]" error message; that
  qualified form is only ever the internal registry's display of available keys, never a valid input
  value. Passing it as input produces the exact same "not found" error for every model including ones
  that definitely work (confirmed by testing `slm-win-nvidia-1gpu`, a model also reachable via
  `tvai_up`, which fails identically) — this cost a lot of debugging time before the real GUI's actual
  command line settled it.
- **`TOPAZ_MODEL_STORE`** must be set explicitly when run standalone (same reasoning as
  `TVAI_MODEL_DIR` for `tvai_up`) — `config.json`'s `topaz_model_store`. On this machine the real
  weight store is on the **Z: drive**, not the default `C:\ProgramData\...` location tvai_up's models
  use — confirmed by watching the real GUI's file reads. Don't assume the classic `tvai_model_dir`
  path also holds these weights.
- **`cwd` must be `neuroserver.exe`'s own directory** when launching it as a subprocess — it resolves
  its own bundled `Lib\site-packages` (torch, etc.) relative to the working directory, not its own exe
  path. Without this it fails immediately with `ModuleNotFoundError: No module named 'torch'`.
- **PATH must have Topaz's own ffmpeg directory prepended.** `neuroserver.exe`'s internal
  post-process step (see below) shells out to a bare `ffmpeg` resolved off `PATH` rather than using
  its own bundled Topaz ffmpeg — on this machine `PATH` otherwise resolves `ffmpeg` to the GPL build
  from winget (`config.json`'s `ffmpeg_libx264`), which has no `tvai_up` filter at all
  (`--enable-tvai` is a Topaz-specific build flag), so that internal step fails outright with
  `Error : Filter not found` instead of the (recoverable) QSV error below.
- **The internal post-process step is reliably broken on this machine and there's no flag to fix it
  directly.** After the actual diffusion pass finishes, `neuroserver.exe` runs a second internal
  `tvai_up` pass (Nyx-3 denoise model) to AI-upscale the raw diffusion output the rest of the way to
  the exact requested tier, then encodes the final file — and that internal ffmpeg call hits the same
  QSV hwaccel-autopick bug as everything else on this NVIDIA-only machine (see "Topaz ffmpeg gotchas"
  below), except here it's Topaz's own internal call with no exposed flag to force `h264_cuvid`.
  **The user independently discovered and validated the workaround in the real GUI first**: the raw,
  pre-post-process diffusion output survives on disk as a `<requested_output_path>.temp.<hash>.mp4`
  sibling file next to the (empty/missing) requested output — it's complete and correct, just missing
  audio (Topaz strips audio before the post-process step). `_run_generative()` automates exactly this:
  run `neuroserver.exe`, swallow the expected `CommandError`, glob for the `.temp.*.mp4` sibling,
  validate its dimensions/existence, then remux the source's original audio back in via a plain
  `-c copy` (no re-encode, no decoder concerns either way since nothing is being decoded).
- **The model's real output height does not necessarily match the requested tier.** Requesting
  `--upscale-factor 3` / `--output-height 1440` on a 480p 4:3 source produced an actual, measured
  1440x1080 result (2.25x, not 3x) — confirmed independently by the user on two separate real GUI
  renders and by our own testing. This looks like the diffusion model has its own native/canonical
  output canvas (1440x1080 for 4:3 content) rather than a scale-relative output. Because of this,
  `_run_generative()` always measures the actual recovered file's dimensions via ffprobe and validates
  against `output_tier_height` as a **minimum**, rather than trusting the requested/nominal tier for
  anything beyond choosing what to ask for.

### Test crop / In-Out points (app/stages/ingest.py)

A job can optionally carry `crop_start_seconds` / `crop_end_seconds` (nullable REAL columns on
`jobs`), set via the dashboard's "Test crop" checkbox or the `/api/jobs` POST body. When set,
`ingest.py` trims the staged copy immediately after copying (`-ss <start> -i <file> -t <duration> -c
copy`) before the rest of the pipeline ever sees it — added specifically to make iterating on the
very slow generative engine practical. Uses `-c copy` (no re-encode) so the cut snaps to the nearest
preceding keyframe rather than being frame-exact — an accepted tradeoff for a testing feature, not
something to "fix" for production accuracy.

### Job control: abort / rerun / failure categories

`worker.py` runs a single background thread processing one job at a time; there's no built-in way to
interrupt a subprocess mid-stage from another thread, so `procutil.py` tracks every `subprocess.Popen`
it starts in a module-level list (`_track`/`_untrack`, guarded by a lock) and exposes `kill_active()`.
`worker.request_cancel(job_id)` uses that to abort the actively-running job (kills its subprocess,
which makes the stage's `run_logged`/`run_piped_logged` call raise `CommandError`, caught by
`_process_one` and — because a matching cancel request is recorded — filed as `status='cancelled'`
rather than `'failed'`) or, for a merely-queued job, marks it cancelled directly with nothing to kill.
`DELETE /api/jobs/{id}` calls this automatically first if the job is running, then cleans up its
staging dir before deleting the row — the dashboard's delete button doubles as "abort and delete."
`POST /api/jobs/{id}/rerun` duplicates a job's *original submission parameters* (not its runtime
settings/current_file) into a brand new job and queues it — the original job/output is untouched.

**Hard-won gotcha**: any code touching the module-level `_cancel_requested_job_id` from inside
`_process_one` needs `global _cancel_requested_job_id` declared in *that function's own scope* — a
missing `global` there caused `UnboundLocalError` the moment any stage completed (success or
failure), which propagated out of `_process_one` uncaught and **silently killed the entire worker
thread** (a daemon thread — Python just prints the traceback to stderr and moves on; the FastAPI
process keeps running as if nothing happened, but every job submitted afterward sits at `'pending'`
forever with no error visible anywhere in the UI). This is exactly the failure mode the project's
"never crash the worker loop" comment on `_process_one`'s except clause was meant to prevent, and it
still happened because that protection only covered `runner(job_id)`, not `_process_one`'s own
bookkeeping code. `_loop()` now also wraps its call to `_process_one` in a defensive try/except as a
second layer, precisely so a future bug in `_process_one` itself can't take the whole worker down
silently again — if you ever see jobs stuck at `'pending'` with a live server and an idle worker
status, check the server's own stdout/stderr for an uncaught thread exception before looking anywhere
else.

`failure_category` (nullable column on `jobs`, set alongside `status='failed'`) is a best-effort
classification of the error message/traceback text (`worker._classify_failure`) into `'oom'` /
`'disk_full'` / `None` — purely a UI label (`server.py`'s `_row_to_dict`/`api_*` return it as-is,
`app.js`'s `statusLabel()` renders "FAILED (OOM)" / "FAILED (Disk Full)"), not a control-flow value;
`status` itself stays `'failed'` either way so existing retry/terminal-state logic doesn't need to
know about it.

## Machine-specific configuration

`config.json`'s `tools`/`vs_plugins`/`tvai_model_dir` are **absolute Windows paths into specific app
installs** (Topaz Video AI, Hybrid, HandBrake) on this one machine, confirmed by direct inspection
(reading each tool's actual model JSONs, testing its CLI flags, etc.) rather than assumed from
documentation. Moving this to another machine means re-locating and re-verifying every one of those
paths — don't assume they're portable defaults.

`nas_root` and `staging_drives` are likewise this machine's actual paths (`\\StudioNAS\...` and a
16GB RAM disk at `R:\` that's deliberately checked for live free space before ever being used, since
it rarely fits a real video file and is volatile across reboots — see `stages/ingest.py`).

## Not in git

`pipeline.db` (job manifest, gitignored) is the runtime state; deleting it loses job history but not
any already-finalized output files (those already moved to their destination folder). `staging/` under
the configured drives holds in-progress files only and is cleaned up per-job by `finalize.py`.
