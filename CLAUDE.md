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

`ingest → probe → deinterlace → denoise → upscale → finalize`. Each stage reads/writes the job's
`settings_json` blob (`db.get_settings`/`db.merge_settings`) to pass detected/computed values forward
(scan type, crop, PAR, per-stage summaries for the metadata embed, etc.) rather than re-deriving them.

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
  If `job.skip_upscale` is set, this becomes the **terminal** stage (see below).
- **denoise** / **upscale**: both start with an early-return no-op when `job.skip_upscale` is set —
  deinterlace already produced and tagged the final file in that mode.
- **upscale** (app/stages/upscale.py + app/topaz_models.py): drives Topaz's own bundled `ffmpeg.exe`
  and its `tvai_up` filter directly (confirmed via `ffmpeg -h filter=tvai_up`). Several non-obvious
  things had to be reverse-engineered here — see "Topaz ffmpeg gotchas" below. `topaz_models.py` is
  shared with `server.py` (for the UI's human-readable preset panel) specifically so the *actual*
  scale-chaining logic and the UI's *description* of it can't drift apart.
- **finalize**: moves the file to the same folder as `original_nas_path` and demangles the name via
  `naming.py`. The original source file is never touched or deleted (it's archival).

### Filename tagging (app/naming.py)

Bracket tags (`[DVD]`, `[480i]`, ...) evolve through the pipeline, but **the terminal output (either
`set_upscaled_tag` or `set_final_progressive_tag`) drops every prior tag and keeps only the single
final one** (e.g. `Show S01E01 [DVD] [480i].mkv` → `Show S01E01 [Upscaled 1080p].mkv`) — deliberate,
not a bug; source-type/interim tags aren't meaningful once the deliverable exists.

### skip_upscale mode

A job-level flag (not a separate pipeline) that fans out across three stages: `denoise`/`upscale`
no-op, and `deinterlace` becomes terminal — handling its own final tagging and the metadata embed that
`upscale.py` would otherwise be responsible for. `ingest.py` also short-circuits entirely (no copy, no
processing) if `skip_upscale` is set and the source filename already carries a progressive-resolution
tag (e.g. `[1080p]`) — nothing to do.

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

The UI exposes one "Film" / "Animation" choice (`content_types.json`); the server resolves that to a
`(topaz_preset, denoise_tune)` pair before creating the job (`server.py`'s `api_create_jobs`) — the
frontend never needs to know individual preset filenames. Presets (`topaz_film.json`,
`topaz_animation.json`) are meant to be hand-tuned over time, not treated as fixed; `resize_flags`,
`tvai_up_params`, and encoder settings are all preset-level knobs.

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
