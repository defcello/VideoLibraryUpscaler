"""Builds the content detector's labeled training manifest from the Disc Library.

    python -m tools.content_detect_manifest <library_root> <files.txt> <manifest.json>

library_root: the folder holding Movies/ and Series/ (the "Disc Library").
files.txt: video paths relative to library_root, one per line, e.g.
    (cd "<library_root>" && find Movies Series -type f -iname "*.mkv" -o -iname "*.mp4" ...)
Feed the result to tools/train_content_detector.py.

label: live | 2d | 3d | mixed | eval:<note>   (eval:* never used for training)
group: title name (train/val split is done per group so no title leaks).
"""
import json, os, random, re, sys
from pathlib import Path

ROOT = Path(sys.argv[1])
FILES = [l.strip() for l in open(sys.argv[2], encoding="utf-8") if l.strip()]
EXCLUDE = re.compile(r"bonus|special feature|extras|deleted|featurette|behind|trailer|making of|\bpanel\b|interview|promo|commentary|menu|\bbg\b|announcement|legend of mordu|history of anime", re.I)
# Hand-checked label fixes (path substring -> label), applied before everything else:
# found by inspecting contact sheets of files the detector disagreed with.
OVERRIDES = {
    "Movies/The Matrix/The Animatrix": "2d",      # anime anthology filed under The Matrix
    "Movies/Moana/Bluray/Moana_t05": "mixed",     # featurette: interviews + 2D concept art + 3D clips
}
random.seed(1234)

LIVE = """Aaja Nachle|Darr|Interstellar|Schindler's List|The Sound of Music|Roman Holiday|Stalag 17|Holiday Inn|You've Got Mail|Inception|The Matrix|Full Metal Jacket|John Wick|Swades|Baazigar|Don [DVD]|Jab We Met|Kuch Kuch|Veer Zaara|Koyla|Yes Boss|Josh [DVD]|Oscar [DVD]|Duplicate|Facing the Giants|Dirty Dancing|Hidalgo|Highlander|Hula Girls|Ip Man|Nacho Libre|Okuribito|Shall We Dance|The Thin Man|White Christmas|The Shop Around the Corner|The Scarlet Pimpernel|Fiddler on the Roof|Indiana Jones|Ben Hur|Babadook|Legend|Tropic Thunder|The Hunger Games|Wonder Woman|The Man With No Name Trilogy_JOHNSURFACE2017_Feb-04-084839|The Interview|The Wiz|The Little Mermaid (2023 Live Action)/2023|Chak De|Sultan|Om Shanti Om|Mohabbatein|Thor~The Dark World|Pirates of the Caribbean|Star Wars|Doctor Strange|Green Lantern|Batman|Dil Bole|Dostana|Rab Ne|Munna Bhai|Billu Barber|The Great Gatsby|Maleficent|Waterworld|Kabhi Khushi|Kabhi Haan|Dil to Pagal|Ghost in the Shell/2017|James Bond""".split("|")
LIVE_SERIES = ["Series/MASH", "Series/Hogans Heroes", "Series/Zorro", "Series/Star Trek", "Series/Sherlock Holmes", "Series/Dead Like Me"]
TWO_D = """Anastasia|Kiki's Delivery Service|Grave of the Fireflies|The Emperor's New Groove|The Hunchback of Notre Dame|The Girl Who Leapt|Mary and the Witch's Flower|Princess Mononoke|Nausicaa|The Secret of Nimh|Tarzan|The Princess and the Frog|The Road to El Dorado|Fantasia/""".split("|")
TWO_D_SERIES = ["Series/Anime/", "Series/My Little Pony", "Series/Over the Garden Wall"]
THREE_D = """Encanto|Soul [Bluray]|Coco/Bluray/|Brave|Finding Nemo|Frozen|Ice Age|Inside Out|Kung Fu Panda|Moana/Bluray/|Monsters Inc|Ratatouille|Tangled|Toy Story|Up/|Wreck-It Ralph|The Incredibles|Turning Red|Megamind|Shrek|How to Train Your Dragon|Final Fantasy""".split("|")
EVAL = {"Corpse Bride": "stopmotion", "The Nightmare Before Christmas": "stopmotion", "Titan A.E": "2d+3d",
        "Avatar": "cgi-live", "Fantasia 2000": "2d+live-hosts", "The Muppet Christmas Carol": "puppets",
        "Origin~Spirits": "anime+3d", "The Lord of the Rings/1978": "rotoscope", "Cypher": "?"}


def title_of(rel):
    parts = rel.split("/")
    if parts[0] == "Movies":
        return re.sub(r" \[.*?\]\..*$|\.\w+$", "", parts[1])
    if parts[1] == "Anime":
        return "Anime/" + parts[2]
    if parts[1] == "Beavis and Butt-Head":
        if len(parts) == 3:
            return "Beavis and Butt-Head/(series root)"
        return "/".join(parts[1:4])  # per-volume, so Volume 4 can be held out as the test set
    return "/".join(parts[1:2])


def match(rel, keys):
    body = rel.split("/", 1)[1]
    return any(body.startswith(k) for k in keys)


def classify(rel):
    for k, lab in OVERRIDES.items():
        if k in rel and not EXCLUDE.search(rel):
            return lab
    if "Beavis and Butt-Head" in rel:
        n = rel.rsplit("/", 1)[1]
        if "MTV Clips" in n or "clips interleaved" in n:
            return "mixed"
        if n.endswith("Story [DVD].mp4") or " - Story " in n:
            return "2d"
        return None
    for k, note in EVAL.items():
        if rel.split("/", 1)[1].startswith(k):
            return "eval:" + note
    if EXCLUDE.search(rel):
        return None
    if match(rel, LIVE) or any(rel.startswith(s) for s in LIVE_SERIES):
        return "live"
    if match(rel, TWO_D) or any(rel.startswith(s) for s in TWO_D_SERIES):
        return "2d"
    if match(rel, THREE_D):
        return "3d"
    return None


by_group = {}
for rel in FILES:
    lab = classify(rel)
    if lab is None:
        continue
    by_group.setdefault((title_of(rel), lab), []).append(rel)

manifest = []
for (group, lab), rels in sorted(by_group.items()):
    is_series = rels[0].startswith("Series/")
    if "Beavis" in group:
        # B&B: plenty of files -- keep a random spread; Vol 4 Disc 1/2 are always kept (user's named test set)
        random.shuffle(rels)
        pick = rels if "Volume 4" in group else rels[:8]
    elif is_series:
        random.shuffle(rels)
        pick = rels[:5]
    else:
        sized = []
        for r in rels:
            try:
                sized.append((os.path.getsize(ROOT / r), r))
            except OSError:
                pass
        sized.sort(reverse=True)
        pick = [r for s, r in sized[:2] if s > 300e6]  # main features only
    for r in pick:
        manifest.append({"path": str(ROOT / r), "label": lab, "group": group, "series": is_series})

json.dump(manifest, open(sys.argv[3], "w", encoding="utf-8"), indent=1)
from collections import Counter
print(Counter(m["label"] for m in manifest))
print(Counter(m["label"] for m in {(m["group"], m["label"]): m for m in manifest}.values()), "groups")
