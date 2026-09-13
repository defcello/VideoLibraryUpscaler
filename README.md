# AI Remaster Pipeline

A local pipeline for AI-upscaling an SD/interlaced video library, following a non-generative
upscaling workflow: **deinterlace/IVTC** → optional **denoise** → optional **dehalo** → **AI
upscale** → **finalize**. A FastAPI backend runs jobs from a SQLite-backed queue on a background
worker thread; a plain-JS dashboard (no build step) shows live progress over SSE.

![Dashboard screenshot](docs/dashboard.png)

The dashboard lets you browse a source library, queue files for processing, toggle each stage on
or off, pick a content-type preset, and watch per-stage progress and encoder logs update live as a
job runs.

## Stages

1. **Deinterlace/IVTC** — Hybrid's bundled QTGMC/VIVTC via VapourSynth
2. **Denoise** — HandBrake NLMeans
3. **Dehalo** — Topaz Iris + Artemis (removes ringing/halo artifacts from oversharpened DVD sources)
4. **Upscale** — Topaz Video AI, driven headlessly via its bundled ffmpeg

Each stage is independently toggleable, and a content-type preset (Film, Film Generative,
Animation) picks the tuned settings for that run.

## Requirements

- **Windows** with an **[NVIDIA GPU](https://www.nvidia.com/en-us/geforce/drivers/)** — the
  pipeline drives Topaz's bundled ffmpeg using hardware `*_nvenc`/`*_cuvid` codecs exclusively;
  there's no software encode/decode fallback.
- **[Python 3.10+](https://www.python.org/downloads/)**, with `pip install -r requirements.txt`
  (FastAPI, Uvicorn, Jinja2).
- **[Topaz Video AI](https://www.topazlabs.com/topaz-video)**, with an active paid subscription —
  optional; only needed if you enable the Upscale and/or Dehalo stages (both use Topaz models, and
  every one of them requires a subscription; there is no perpetual-license tier that unlocks them).
  The pipeline runs fine without Topaz installed as long as both stages stay off.
- **[Hybrid](https://www.selur.de/downloads)** (Selur's VapourSynth-based deinterlacer/encoder
  GUI) — optional; only needed if you enable the Deinterlace/IVTC stage. Installed for its
  bundled, portable VapourSynth runtime, VSPipe, and QTGMC/VIVTC filter DLLs, which this project
  drives directly so results match what Hybrid's own GUI would produce.
- **[HandBrakeCLI](https://handbrake.fr/downloads2.php)** — optional; only needed if you enable
  the Denoise stage (the pipeline runs fine without it as long as Denoise stays off). This is the
  separate command-line download, not the regular HandBrake GUI installer — the GUI app doesn't
  include the `HandBrakeCLI.exe` binary this stage shells out to.

Every stage above is independently toggleable in the dashboard, and its underlying tool is only
touched when that toggle is on — so in principle you only need to install whichever tools back the
stages you actually plan to use.

All tool paths live in `config.json` and are currently hardcoded to one specific Windows machine's
install locations — see "Machine-specific configuration" in [CLAUDE.md](CLAUDE.md) before running
this anywhere else.

## Running it

```bash
pip install -r requirements.txt
python -m app.server          # starts the server at http://127.0.0.1:8756
start_pipeline.bat            # same, plus opens the dashboard in Brave
```

There's no build step or automated test suite — verify changes by starting the server and running
a short clip through the dashboard.

## Notes

This is tightly coupled to tool installs on one specific Windows machine. See
[CLAUDE.md](CLAUDE.md) for the full architecture, the job state machine, and a long list of
hard-won gotchas discovered while building this.
