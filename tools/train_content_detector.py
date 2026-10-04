"""Trains the content-type detector's linear head (app/models/content_head.npz).

    python -m tools.train_content_detector extract  <manifest.json> <cache_dir>
    python -m tools.train_content_detector train    <manifest.json> <cache_dir>
    python -m tools.train_content_detector evaluate <manifest.json> <cache_dir>

manifest.json is [{"path", "label", "group"}] where label is live / 2d / 3d
(frame-level training classes), mixed (file-level only -- used to tune the
Mixed threshold, never as a frame class), or eval:<note> (reported, never
trained on). tools/content_detect_manifest.py builds one from the Disc
Library. `group` is the title: validation holds out whole titles, so a
movie's frames are never split across train and validation.

`extract` caches each file's frame embeddings (and small JPEG thumbnails for
eyeballing misclassifications) under cache_dir, so retraining is instant.

`train` also writes app/models/content_head.index.json next to the model: every
source file (library-relative path, label, title group, train vs evaluation-only) with the
exact timestamp and frame number of every frame used, plus the sampling and
training settings -- enough to rebuild the training set from the library, or
just to see what the shipped model learned from.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from app import content_detect as cd

CLASSES = cd.FRAME_CLASSES
# Aggregation/sampling knobs shipped inside the head file (see
# content_detect.aggregate/detect). Chosen by grid search over the same
# title-held-out splits `train` reports (plus Mixed recall from a head trained
# with NO Beavis and Butt-Head at all): best workflow accuracy with 100% Mixed
# recall was min_frame_conf 0.6, mixed_min_share 0.07, mixed_min_segments 3.
DEFAULT_PARAMS = {
    "min_frame_conf": 0.6, "min_frames": 8,
    "mixed_min_share": 0.07, "mixed_min_segments": 3,
    "initial_frames": 32, "max_frames": 256, "min_spacing_seconds": 2.0,
}


def _key(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def _load_manifest(p):
    return json.load(open(p, encoding="utf-8"))


# ------------------------------------------------------------------ extract

def extract(manifest_path, cache_dir):
    import cv2
    cache = Path(cache_dir)
    (cache / "thumbs").mkdir(parents=True, exist_ok=True)
    items = _load_manifest(manifest_path)
    embed_lock = threading.Lock()
    done = [0]

    def one(item):
        out = cache / f"{_key(item['path'])}.npz"
        if out.exists():
            return
        trainable = item["label"] in CLASSES
        n = None if not trainable else (24 if item.get("series") else 40)
        try:
            frames = cd.sample_frames(Path(item["path"]), n=n, workers=4)
        except Exception as e:  # noqa: BLE001 -- one bad file shouldn't stop the run
            print(f"  ! {item['path']}: {e}", flush=True)
            return
        with embed_lock:
            emb = cd.embed([f for _, f in frames])
        for i, (ts, f) in enumerate(frames):
            h = 96
            cv2.imwrite(str(cache / "thumbs" / f"{_key(item['path'])}_{i:03d}.jpg"),
                        cv2.resize(f, (max(1, int(f.shape[1] * h / f.shape[0])), h)))
        np.savez(out, emb=emb.astype(np.float16), ts=np.array([ts for ts, _ in frames]))
        done[0] += 1
        print(f"  [{done[0]}] {len(frames):3d} frames  {item['label']:6s} {Path(item['path']).name}", flush=True)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, items))


def _load_cached(items, cache):
    for it in items:
        f = Path(cache) / f"{_key(it['path'])}.npz"
        if f.exists():
            z = np.load(f)
            yield it, z["emb"].astype(np.float32), z["ts"]


# -------------------------------------------------------------------- train

FIT_C = 1.0  # inverse L2 strength; chosen by cross-validation (see train's report)


def _fit(X, y, groups, C=None):
    """Multinomial logistic regression (scikit-learn L-BFGS -- a hand-rolled
    fixed-step gradient descent used originally was unstable: one split
    collapsed to calling every live-action title 2D). Sample weights balance
    classes AND titles, so one long anime series or the sheer number of B&B
    files can't dominate what '2d' means. Returns (W, b, mean) in the plain
    numpy form content_detect.frame_probs uses -- sklearn is training-only."""
    from sklearn.linear_model import LogisticRegression
    mean = X.mean(axis=0)
    w = np.ones(len(y))
    for c in range(len(CLASSES)):
        cls_groups = {g for g, yy in zip(groups, y) if yy == c}
        for g in cls_groups:
            m = (groups == g) & (y == c)
            w[m] = 1.0 / (m.sum() * len(cls_groups))
    w *= len(y) / w.sum()
    clf = LogisticRegression(C=FIT_C if C is None else C, max_iter=5000)
    clf.fit(X - mean, y, sample_weight=w)
    return clf.coef_.T.copy(), clf.intercept_.copy(), mean


def _frames(items, cache):
    X, y, g = [], [], []
    for it, emb, _ in _load_cached(items, cache):
        if it["label"] in CLASSES and len(emb):
            X.append(emb); y += [CLASSES.index(it["label"])] * len(emb); g += [it["group"]] * len(emb)
    return np.concatenate(X), np.array(y), np.array(g)


def _file_results(items, cache, head, params):
    rows = []
    for it, emb, ts in _load_cached(items, cache):
        probs = cd.frame_probs(emb, head)
        rows.append((it, cd.aggregate(probs, ts, params)))
    return rows


def _expected(label):
    return {"live": "live", "2d": "2d", "3d": "3d", "mixed": "mixed"}.get(label)


def _report(rows, title):
    print(f"\n== {title}")
    by = {}
    for it, r in rows:
        exp = _expected(it["label"])
        if exp is None:
            continue
        by.setdefault(exp, [0, 0])
        by[exp][1] += 1
        by[exp][0] += r["result"] == exp
    tot = sum(v[1] for v in by.values()); ok = sum(v[0] for v in by.values())
    for k, (a, n) in sorted(by.items()):
        print(f"   {k:6s} {a}/{n}  ({a / n:.0%})")
    if tot:
        print(f"   ALL    {ok}/{tot}  ({ok / tot:.0%})")
    # What actually matters: only 2D vs everything-else picks a different workflow.
    wf = [(it, r) for it, r in rows if _expected(it["label"])]
    wf_ok = sum((_expected(it["label"]) == "2d") == (r["result"] == "2d") for it, r in wf)
    if wf:
        print(f"   WORKFLOW (Animation vs Film) {wf_ok}/{len(wf)}  ({wf_ok / len(wf):.1%})")
    for it, r in rows:
        exp = _expected(it["label"])
        if exp is None or r["result"] != exp:
            tag = "EVAL" if exp is None else "MISS"
            print(f"   {tag} [{it['label']}] -> {r['result']} {r['shares']}  {Path(it['path']).name}")


def _split(items, val_frac=0.3, seed=7):
    """Title-level split, stratified by class."""
    rng = np.random.default_rng(seed)
    val = set()
    for c in CLASSES:
        gs = sorted({it["group"] for it in items if it["label"] == c})
        rng.shuffle(gs)
        val |= set(gs[: max(1, int(len(gs) * val_frac))])
    return val


def train(manifest_path, cache_dir, out_path=cd.HEAD_PATH):
    items = _load_manifest(manifest_path)
    params = dict(DEFAULT_PARAMS)

    # 1) Honest estimate: 3 different title-level splits, train on the rest.
    for seed in (7, 8, 9):
        val = _split(items, seed=seed)
        tr = [it for it in items if it["group"] not in val]
        X, y, g = _frames(tr, cache_dir)
        W, b, mean = _fit(X, y, g)
        head = {"W": W, "b": b, "mean": mean}
        held = [it for it in items if it["group"] in val and it["label"] in CLASSES]
        Xv, yv, _ = _frames(held, cache_dir)
        acc = (cd.frame_probs(Xv, head).argmax(axis=1) == yv).mean()
        print(f"\nsplit seed={seed}: {len(val)} held-out titles, frame accuracy {acc:.1%}")
        _report(_file_results(held, cache_dir, head, params), f"held-out titles (seed {seed})")

    # 2) Honest Mixed check: a head that has never seen Beavis and Butt-Head
    #    (the Mixed examples' own art style) at all.
    X, y, g = _frames([it for it in items if "Beavis" not in it["group"]], cache_dir)
    W, b, mean = _fit(X, y, g)
    _report(_file_results([it for it in items if it["label"] == "mixed"], cache_dir,
                          {"W": W, "b": b, "mean": mean}, params), "mixed files, head trained with NO Beavis and Butt-Head")

    # 3) Final head on everything trainable; mixed/eval files were never trained on.
    X, y, g = _frames(items, cache_dir)
    W, b, mean = _fit(X, y, g)
    head = {"W": W, "b": b, "mean": mean}
    _report(_file_results([it for it in items if not it["label"] in CLASSES], cache_dir, head, params),
            "mixed + eval files (never trained on)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out_path, W=W.astype(np.float32), b=b.astype(np.float32), mean=mean.astype(np.float32),
             params=json.dumps(params))
    print(f"\nwrote {out_path} ({len(y)} training frames)")
    _write_index(items, cache_dir, out_path.with_suffix(".index.json"), params)


def _fps(path: str):
    import subprocess
    proc = subprocess.run([cd._FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=avg_frame_rate,r_frame_rate", "-of", "json", path],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        st = json.loads(proc.stdout)["streams"][0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            num, _, den = st[key].partition("/")
            if float(num) and float(den or 1):
                return float(num) / float(den or 1)
    except (ValueError, KeyError, IndexError):
        pass
    return None


def _library_relative(path: str) -> str:
    """'\\\\server\\share\\...\\Disc Library\\Series\\X\\y.mkv' -> 'Series/X/y.mkv'. The
    index is committed, so it never records where the library actually lives
    -- a rebuild prefixes its own library root."""
    parts = path.replace("\\", "/").split("/")
    for i, part in enumerate(parts):
        if part in ("Movies", "Series"):
            return "/".join(parts[i:])
    raise ValueError(f"path is not under a Movies/ or Series/ folder: {Path(path).name}")


def _write_index(items, cache_dir, index_path, params):
    """Records exactly which frames of which files the shipped head saw."""
    import datetime
    cached = list(_load_cached(items, cache_dir))
    with ThreadPoolExecutor(max_workers=8) as ex:
        fps = list(ex.map(lambda row: _fps(row[0]["path"]), cached))
    files = []
    for (it, emb, ts), rate in zip(cached, fps):
        t = sorted(round(float(x), 3) for x in ts)
        files.append({
            "path": _library_relative(it["path"]), "label": it["label"], "group": it["group"],
            "use": "train" if it["label"] in CLASSES else "evaluation_only",
            "fps": round(rate, 4) if rate else None,
            "t": t,
            "frame": [int(round(x * rate)) for x in t] if rate else None,
        })
    index = {
        "description": "Training/evaluation source index for content_head.npz. 'use: train' files' frames "
                       "(at seconds 't' / 0-based frame numbers 'frame') are the model's training data; "
                       "'evaluation_only' files (Mixed examples, hard cases) were only scored, never trained on. "
                       "Paths are relative to the library root (the folder holding Movies/ and Series/). "
                       "Rebuild with tools/content_detect_manifest.py + tools/train_content_detector.py.",
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "clip": {"arch": cd.CLIP_ARCH, "pretrained": cd.CLIP_PRETRAINED},
        "sampling": {
            "decoder": "GPL ffmpeg (config tools.ffmpeg_libx264), input seek: -ss <t> -i <file> -frames:v 1",
            "scale": f"scale='if(gt(sar,0),iw*sar,iw)':ih,scale=-2:{cd._SAMPLE_HEIGHT}",
            "black_reject": {k: cd.CONFIG["blackdetect"][k] for k in ("pix_th", "pic_th")},
            "black_retry_seconds": cd.BLACK_RETRY_SECONDS,
            "then": "letterbox bars trimmed; fades / flat frames dropped (content_detect.is_informative)",
        },
        "training": {"classes": list(CLASSES), "classifier": "sklearn LogisticRegression (multinomial, L-BFGS)",
                     "C": FIT_C, "sample_weights": "balanced per class and per title group"},
        "params": params,
        "counts": {
            "files": len(files),
            "train_frames": sum(len(f["t"]) for f in files if f["use"] == "train"),
            "files_by_label": {lab: sum(1 for f in files if f["label"] == lab) for lab in sorted({f["label"] for f in files})},
        },
        "files": files,
    }
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=1)
    print(f"wrote {index_path} ({len(files)} files, {index['counts']['train_frames']} training frames)")


def evaluate(manifest_path, cache_dir):
    items = _load_manifest(manifest_path)
    head = cd.load_head()
    _report(_file_results(items, cache_dir, head, head["params"]), "all files, shipped head (train files included)")


if __name__ == "__main__":
    cmd, manifest, cache = sys.argv[1:4]
    {"extract": extract, "train": train, "evaluate": evaluate}[cmd](manifest, cache)
