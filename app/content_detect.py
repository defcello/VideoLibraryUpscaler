"""Content-type auto-detection: Live Action / 2D Graphics / 3D Graphics / Mixed.

How it works:
  1. Sample frames evenly across the file (one fast keyframe-seek ffmpeg call
     per frame, run a few at a time), trim letterbox/pillarbox bars, and drop
     frames with nothing to look at (black, fades, near-flat cards).
  2. Embed each frame with CLIP (open_clip ViT-B-32, CPU-only on purpose so
     it never competes with Topaz for VRAM).
  3. A small linear head trained on the Disc Library itself
     (tools/train_content_detector.py -> app/models/content_head.npz) turns
     each embedding into live / 2d / 3d probabilities.
  4. Per-file aggregation: share of confident frames per class. A file is
     "mixed" when the minority side (live vs animated) is both a substantial
     share AND shows up as several separate stretches of the timeline (e.g.
     Beavis and Butt-Head's MTV Clips files) -- a misread shot or two in a
     live film is one stretch, real mixed content keeps coming back.
     Otherwise the majority wins, with 2D vs 3D decided among animated frames.
  5. Sequential sampling: since we have the whole video, not one screenshot,
     detect() starts with a coarse pass and keeps doubling the sampling
     density (new frames at the midpoints of the existing ones) until a 95%
     Wilson interval puts each deciding share clearly on one side of its
     threshold, or the frame budget runs out.

Only 2D-vs-everything-else changes the workflow (Live Action, 3D and Mixed all
use Film), so that's the boundary the thresholds are tuned to protect.

Frame sampling uses the full GPL ffmpeg build (config.json's
tools.ffmpeg_libx264), not Topaz's: Topaz's build has no software H.264/HEVC
decoder, and OpenCV's frame-accurate seeking turned out to be pathologically
slow on some MPEG-2 DVD remuxes (90s+ for 20 frames vs ~5s here).
"""
from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .config import CONFIG, PROJECT_ROOT

HEAD_PATH = PROJECT_ROOT / "app" / "models" / "content_head.npz"
CLIP_ARCH = "ViT-B-32"
CLIP_PRETRAINED = "laion2b_s34b_b79k"

FRAME_CLASSES = ("live", "2d", "3d")
# Detector result -> content_types.json key.
RESULT_TO_CONTENT_TYPE = {"live": "live_action", "2d": "2d", "3d": "3d", "mixed": "mixed"}

_FFMPEG = CONFIG["tools"]["ffmpeg_libx264"]
_FFPROBE = str(Path(_FFMPEG).with_name("ffprobe.exe"))
_SAMPLE_HEIGHT = 288
BLACK_RETRY_SECONDS = 1.5

_model_lock = threading.Lock()
_model = None
_preprocess = None


# ------------------------------------------------------------------ frames

def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [_FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"couldn't read duration of {path}: {proc.stderr[-500:]}")


def _grab_frame(path: Path, ts: float) -> Optional[np.ndarray]:
    import cv2
    # Fast input seek (-ss before -i) lands on the nearest keyframe -- exact
    # timestamps don't matter for sampling. SAR-correct so anamorphic DVD
    # frames aren't horizontally squeezed going into CLIP.
    proc = subprocess.run(
        [_FFMPEG, "-v", "error", "-ss", f"{ts:.3f}", "-i", str(path), "-frames:v", "1", "-an", "-sn",
         "-vf", f"scale='if(gt(sar,0),iw*sar,iw)':ih,scale=-2:{_SAMPLE_HEIGHT}",
         "-f", "image2pipe", "-vcodec", "bmp", "-"],
        capture_output=True,
    )
    if not proc.stdout:
        return None
    img = cv2.imdecode(np.frombuffer(proc.stdout, np.uint8), cv2.IMREAD_COLOR)
    return img


def _trim_bars(img: np.ndarray) -> np.ndarray:
    gray = img.mean(axis=2)
    rows = np.where(gray.mean(axis=1) > 20)[0]
    cols = np.where(gray.mean(axis=0) > 20)[0]
    if len(rows) == 0 or len(cols) == 0:
        return img[:0, :0]
    return img[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def is_black(img: np.ndarray) -> bool:
    """Same definition as the pipeline's ffmpeg `blackdetect` pass
    (config.json's blackdetect pix_th / pic_th, also used for generative
    checkpoint boundaries): a pixel is black when its luma is at or below
    pix_th of full scale, and a frame is black when at least pic_th of its
    pixels are. Applied per sampled frame instead of running the filter over
    the whole file -- we only need a verdict on the few frames we sample."""
    bd = CONFIG["blackdetect"]
    luma = img[:, :, 2] * 0.299 + img[:, :, 1] * 0.587 + img[:, :, 0] * 0.114  # BGR
    return float((luma <= bd["pix_th"] * 255).mean()) >= bd["pic_th"]


def is_informative(img: np.ndarray) -> bool:
    """False for fades, near-flat title cards and slivers (black frames are
    rejected earlier by is_black, before letterbox trimming)."""
    if img.shape[0] < 64 or img.shape[1] < 64:
        return False
    gray = img.mean(axis=2)
    return gray.std() >= 14 and gray.mean() >= 24


def grid(duration: float, n: int) -> list[float]:
    """n evenly spaced timestamps between 2% and 98% of the runtime (skips most
    logos and end credits), each centered in its slot."""
    return [duration * (0.02 + 0.96 * (i + 0.5) / n) for i in range(n)]


def sample_frames(path: Path, n: Optional[int] = None, workers: int = 4,
                  cancelled: Callable[[], bool] = lambda: False,
                  stamps: Optional[list[float]] = None) -> list[tuple[float, np.ndarray]]:
    """Returns [(timestamp, trimmed BGR frame)] for informative frames only --
    at `stamps` if given, else an even grid of n frames."""
    if stamps is None:
        stamps = grid(probe_duration(path), n or 48)

    def one(ts):
        # A sample landing on a black inter-scene gap is nudged forward once
        # rather than dropped -- these gaps are short, and losing the sample
        # would punch a hole in the timeline the Mixed segment count relies on.
        for attempt_ts in (ts, ts + BLACK_RETRY_SECONDS):
            if cancelled():
                return ts, None
            img = _grab_frame(path, attempt_ts)
            if img is None:
                return ts, None
            if not is_black(img):
                img = _trim_bars(img)
                return attempt_ts, (img if is_informative(img) else None)
        return ts, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, stamps))
    return [(ts, img) for ts, img in results if img is not None]


# -------------------------------------------------------------- embeddings

def _load_clip():
    global _model, _preprocess
    with _model_lock:
        if _model is None:
            import open_clip
            import torch
            torch.set_num_threads(max(1, min(8, (__import__("os").cpu_count() or 4) // 2)))
            _model, _, _preprocess = open_clip.create_model_and_transforms(CLIP_ARCH, pretrained=CLIP_PRETRAINED)
            _model.eval()
    return _model, _preprocess


def embed(frames: list[np.ndarray], batch: int = 32) -> np.ndarray:
    """L2-normalized CLIP image embeddings, shape (len(frames), 512)."""
    import torch
    from PIL import Image
    if not frames:
        return np.zeros((0, 512), np.float32)
    model, preprocess = _load_clip()
    out = []
    with torch.no_grad():
        for i in range(0, len(frames), batch):
            x = torch.stack([preprocess(Image.fromarray(f[:, :, ::-1])) for f in frames[i:i + batch]])
            e = model.encode_image(x).float()
            out.append((e / e.norm(dim=-1, keepdim=True)).numpy())
    return np.concatenate(out)


# --------------------------------------------------------- classification

_head_cache: Optional[dict] = None


def load_head() -> dict:
    global _head_cache
    if _head_cache is None:
        z = np.load(HEAD_PATH, allow_pickle=False)
        _head_cache = {
            "W": z["W"], "b": z["b"], "mean": z["mean"],
            "params": json.loads(str(z["params"])),
        }
    return _head_cache


def frame_probs(emb: np.ndarray, head: Optional[dict] = None) -> np.ndarray:
    head = head or load_head()
    logits = (emb - head["mean"]) @ head["W"] + head["b"]
    logits -= logits.max(axis=1, keepdims=True)
    p = np.exp(logits)
    return p / p.sum(axis=1, keepdims=True)


def _wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def aggregate(probs: np.ndarray, stamps, params: dict) -> dict:
    """Per-file decision from per-frame probabilities (columns =
    FRAME_CLASSES) at the given timestamps. 'settled' says whether the
    decision is statistically clear, i.e. more frames are unlikely to flip it."""
    n = len(probs)
    if n == 0:
        return {"result": None, "reason": "no informative frames", "shares": {}, "frames": 0, "settled": False}
    probs = probs[np.argsort(np.asarray(stamps))]
    conf = probs.max(axis=1) >= params["min_frame_conf"]
    top = probs.argmax(axis=1)
    counted = int(conf.sum())
    if counted < params["min_frames"]:
        return {"result": None, "reason": f"only {counted} confident frame(s)", "shares": {},
                "frames": n, "confident_frames": counted, "settled": False}
    k_live = int(((top == 0) & conf).sum())
    k_2d = int(((top == 1) & conf).sum())
    k_3d = int(((top == 2) & conf).sum())
    k_anim = k_2d + k_3d
    minority = (top == 0) if k_live < k_anim else (top > 0)

    # Separate stretches of minority frames: runs broken by at least one
    # confident majority frame (unsure frames neither break nor extend a run).
    segments, prev = 0, False
    for is_min, is_conf in zip(minority, conf):
        if not is_conf:
            continue
        if is_min and not prev:
            segments += 1
        prev = bool(is_min)

    tau = params["mixed_min_share"]
    k_min = min(k_live, k_anim)
    lo, hi = _wilson(k_min, counted)
    is_mixed = k_min / counted >= tau and segments >= params["mixed_min_segments"]
    # Settled once the minority share's interval is clear of tau. A mixed
    # call only needs to be clear of half of tau -- real mixed content sits
    # far above tau anyway (its 5th percentile is ~1.5x tau).
    mixed_settled = (lo >= tau * 0.5) if is_mixed else (lo >= tau or hi < tau)

    if is_mixed:
        result, confidence, settled = "mixed", min(1.0, (k_min / counted) / (2 * tau)), mixed_settled
    elif k_live >= k_anim:
        result, confidence, settled = "live", k_live / counted, mixed_settled
    else:
        # 2D vs 3D among the animated frames.
        k_major = max(k_2d, k_3d)
        result = "2d" if k_2d >= k_3d else "3d"
        confidence = k_major / counted
        settled = mixed_settled and _wilson(k_major, k_anim)[0] > 0.5
    return {
        "result": result,
        "shares": {"live": round(k_live / counted, 3), "2d": round(k_2d / counted, 3), "3d": round(k_3d / counted, 3)},
        "minority_segments": segments,
        "frames": n,
        "confident_frames": counted,
        "confidence": round(float(confidence), 3),
        "settled": bool(settled),
    }


def detect(path: Path, log: Callable[[str], None] = print,
           cancelled: Callable[[], bool] = lambda: False) -> dict:
    """Classifies a video file with sequential sampling (see module doc).
    Returns aggregate()'s dict plus 'content_type' (a content_types.json key),
    'rounds', and a per-frame 'timeline' [(seconds, class)] for logging."""
    head = load_head()
    params = head["params"]
    duration = probe_duration(path)
    n = params["initial_frames"]
    stamps = grid(duration, n)
    tried: list[float] = []       # every timestamp sampled, informative or not
    stamps_all: list[float] = []  # informative ones, parallel to embs rows
    embs: list[np.ndarray] = []
    rounds = 0
    while True:
        rounds += 1
        tried += stamps
        frames = sample_frames(path, stamps=stamps, cancelled=cancelled)
        if frames:
            stamps_all += [ts for ts, _ in frames]
            embs.append(embed([img for _, img in frames]))
        probs = frame_probs(np.concatenate(embs), head) if embs else np.zeros((0, 3))
        out = aggregate(probs, stamps_all, params)
        log(f"content detect round {rounds}: +{len(stamps)} sampled, {out['frames']} informative so far -> "
            f"{out['result']} {out.get('shares', {})} minority segments={out.get('minority_segments', '-')} "
            f"({'settled' if out['settled'] else 'unsettled'})")
        # Next round doubles density: the midpoints between every existing sample.
        next_n = 2 * n
        if (out["settled"] or cancelled() or next_n > params["max_frames"]
                or duration / next_n < params["min_spacing_seconds"]):
            break
        stamps = _midpoints(sorted(tried))
        n = next_n
    out["rounds"] = rounds
    out["content_type"] = RESULT_TO_CONTENT_TYPE.get(out["result"]) if out["result"] else None
    order = np.argsort(stamps_all)
    out["timeline"] = [[round(stamps_all[i], 1), FRAME_CLASSES[int(probs[i].argmax())]] for i in order]
    return out


def _midpoints(stamps: list[float]) -> list[float]:
    """Timestamps halfway between each pair of consecutive already-tried
    timestamps -- doubles sampling density without repeating a frame."""
    return [(a + b) / 2 for a, b in zip(stamps, stamps[1:])]


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        r = detect(Path(p), log=lambda m: None)
        r.pop("timeline")
        print(json.dumps(r), Path(p).name)
