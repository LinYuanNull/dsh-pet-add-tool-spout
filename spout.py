#!/usr/bin/env python3
"""spout — hatch a living dsh-pet out of a folder of static pose stills.

    static poses  ->  knocked-out alpha  ->  shared canvas  ->  breathing motion
                  ->  frames2d tracks    ->  pet.json        ->  $DSH_HOME/pets/<id>

Most "pet" art arrives as a handful of flat stills: one picture per mood, painted
on a white backdrop, with no alpha and nothing to animate. dsh-pet's `frames2d`
renderer can play those as a pet, but only if the stills are turned into a
directory of frame files with a manifest that maps the seven activity phases onto
tracks. spout does exactly that, and refuses to ship a pet it cannot verify.

Why the alpha work is not a one-liner
-------------------------------------
Knocking out a flat backdrop by brightness alone punches holes straight through
the character: faces, skin and white clothing are near-white too, so they read as
"background" and the pet's face turns transparent against the GUI's dark chrome.
spout therefore defines background by *reachability* instead — a region is
background only when it can be reached from the image border through non-subject
pixels. Anything enclosed by line art (that face, that shirt) stays part of the
subject and is forced fully opaque. Only the outermost few pixels keep a soft ramp
so the silhouette stays anti-aliased without growing a white fringe.

Every hatch is then checked, not assumed: `validate` re-runs the plugin's own
registry code over the result and audits each frame for see-through interiors.

Commands
--------
    spout states   <src>                     list how poses were discovered
    spout mask     <image> --out <png>       knock out one image (debug helper)
    spout hatch    <src> --id ID ...         build a pet directory
    spout install  <petdir> [--select]       copy it into $DSH_HOME/pets/<id>/
    spout validate <petdir>                  structure + alpha + plugin contract

Requires: Pillow, numpy. The plugin-contract check additionally needs Node >= 22.6
(TS type stripping) and an installed copy of @linxin666/dsh-pet; it degrades to a
warning when either is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

try:
    import numpy as np
    from PIL import Image, ImageFilter
except ImportError as exc:  # pragma: no cover - dependency guard
    sys.exit(f"spout: missing dependency ({exc.name}). Install with: pip install pillow numpy")

# Windows consoles default to a legacy code page, which mangles the em dashes and
# CJK display names this tool prints. Emit UTF-8 regardless of the code page.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - exotic stream
            pass

NAME = "spout"
VERSION = "1.0.0"

# --------------------------------------------------------------------------- #
# The dsh-pet contract this tool writes against.
# --------------------------------------------------------------------------- #

# Fixed atlas/frame row order of the 9-state animation contract.
ROW_ORDER = [
    "idle",
    "running-right",
    "running-left",
    "waving",
    "jumping",
    "failed",
    "waiting",
    "running",
    "review",
]

# Default ActivityPhase -> track mapping (identity, which is why tracks share names).
PHASE_TRACK = {
    "idle": "idle",
    "waiting": "waiting",
    "thinking": "running",
    "tool": "running-right",
    "review": "review",
    "done": "waving",
    "failed": "failed",
}

# File-name aliases accepted for each contract state.
POSE_ALIASES = {
    "idle": ["idle", "stand", "standing", "default", "rest"],
    "running-right": ["running-right", "runningright", "run-right", "runright", "right"],
    "running-left": ["running-left", "runningleft", "run-left", "runleft", "left"],
    "waving": ["waving", "wave", "hello", "hi", "greet"],
    "jumping": ["jumping", "jump", "happy", "celebrate", "cheer"],
    "failed": ["failed", "fail", "failure", "error", "sad", "broken"],
    "waiting": ["waiting", "wait", "bored", "patience"],
    "running": ["running", "run", "thinking", "think", "busy"],
    "review": ["review", "reviewing", "inspect", "check", "reading"],
}

IMAGE_EXTENSIONS = {".png", ".webp", ".gif", ".jpg", ".jpeg", ".bmp"}
MIN_FRAME_MS, MAX_FRAME_MS = 16, 5000
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# --------------------------------------------------------------------------- #
# Motion presets
#
# A key is one output frame: the pose is offset by (dy) px, scaled by (sx, sy)
# about its footing (bottom-centre) and rotated by (angle) degrees about the same
# point. Amplitudes are tuned for a pet rendered at ~160 px, so they read at the
# GUI's real display size rather than only at frame resolution.
# --------------------------------------------------------------------------- #


def _breath(keys, *, ms, sx_from_sy=None):
    """Breathing keys given as (dy, sy, angle); sx follows from sy when asked."""
    out = []
    for dy, sy, angle in keys:
        key = {"dy": dy, "sy": sy, "angle": angle}
        if sx_from_sy is not None:
            key["sx"] = round(1 + (1 - sy) * sx_from_sy, 4)
        out.append(key)
    return {"keys": out, "ms": ms}


def _sway(keys, *, ms):
    """Sway keys given as (dy, angle) with no scale change."""
    return {"keys": [{"dy": dy, "angle": angle} for dy, angle in keys], "ms": ms}


PRESETS = {
    # The default rhythm: every track breathes, leans or bobs.
    "default": {
        "idle": {"pose": "idle", **_breath(
            [(-3.2, 0.994, -0.9), (-6.0, 0.988, -1.5), (-6.6, 0.986, -1.2), (-5.0, 0.989, -0.4),
             (-2.0, 0.995, 0.5), (0.8, 1.002, 1.1), (2.2, 1.005, 1.2), (0.9, 1.002, 0.4)],
            ms=200, sx_from_sy=0.4)},
        "waiting": {"pose": "idle", **_breath(
            [(-1.8, 0.998, -0.6), (-3.8, 0.994, -1.0), (-4.6, 0.993, -0.7),
             (-3.0, 0.995, 0.1), (0.4, 1.0, 0.7), (1.2, 1.002, 0.5)],
            ms=240, sx_from_sy=0.35)},
        "running": {"pose": "running", **_sway(
            [(-3.0, -2.0), (-1.4, -0.4), (0.2, 1.2), (-1.2, 2.0), (-2.6, 0.8), (-1.0, -1.0)],
            ms=240)},
        "running-right": {"pose": "running-right", **_sway(
            [(-2.0, -3.2), (-1.2, -0.6), (-2.8, 1.8), (-0.6, 2.0),
             (-1.8, 0.6), (-0.9, -1.4), (-1.4, -2.4), (0.5, -0.8)],
            ms=190)},
        "running-left": {"pose": "running-left", **_sway(
            [(-2.2, 3.0), (-0.9, 1.6), (-2.6, -1.2), (-0.5, -2.2),
             (-1.9, -0.8), (-0.8, 1.2), (-1.3, 2.2), (0.6, 1.0)],
            ms=190)},
        "waving": {"pose": "waving", **_sway(
            [(0.0, 0.0), (-1.0, -1.5), (0.0, 0.0), (1.0, 1.5), (0.0, 0.0)], ms=260)},
        # A crouch, a hop and a landing, so `done` reads as a real jump.
        "jumping": {"pose": "jumping", "ms": 130, "keys": [
            {"dy": 4, "sy": 0.94, "sx": 1.06, "ms": 130},
            {"dy": 0, "ms": 130},
            {"dy": -16, "sy": 1.03, "ms": 150},
            {"dy": -24, "sy": 1.04, "sx": 0.97, "ms": 120},
            {"dy": 4, "ms": 150},
            {"dy": 1, "sy": 0.97, "sx": 1.02, "ms": 130},
        ]},
        "review": {"pose": "review", **_sway(
            [(-0.8, -1.6), (0.2, -0.2), (0.8, 1.2), (0.2, 2.0),
             (-0.6, 1.0), (-1.0, -0.4), (0.2, -1.2)],
            ms=300)},
        "failed": {"pose": "failed", "ms": 200, "keys": [
            {"dy": 1.5, "angle": 1.8, "ms": 200},
            {"dy": 3.0, "angle": 3.0, "ms": 300},
            {"dy": 3.2, "angle": 3.2, "ms": 300},
            {"dy": 3.2, "angle": 3.2, "ms": 400},
            {"dy": 3.0, "angle": 3.0, "ms": 300},
            {"dy": 1.6, "angle": 1.8, "ms": 200},
        ]},
    },
    # One static frame per track: correct, but nothing moves.
    "still": {state: {"pose": state, "ms": 400, "keys": [{}]} for state in ROW_ORDER},
}

PRESET_ALIASES = {"none": "still", "static": "still", "off": "still"}


# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #

_VERBOSE = False


def log(message):
    print(message, flush=True)


def detail(message):
    if _VERBOSE:
        print("      " + message, flush=True)


def ok(label, extra=""):
    print(f"  PASS  {label}" + (f"  ({extra})" if extra else ""), flush=True)


def bad(label, extra=""):
    print(f"  FAIL  {label}" + (f"  ({extra})" if extra else ""), flush=True)


def warn(label):
    print(f"  WARN  {label}", flush=True)


# --------------------------------------------------------------------------- #
# Masking: flat-backdrop knock-out with hole filling
# --------------------------------------------------------------------------- #


def _shift(mask, dy, dx):
    out = np.zeros_like(mask)
    h, w = mask.shape
    ys0, ys1 = max(0, dy), min(h, h + dy)
    xs0, xs1 = max(0, dx), min(w, w + dx)
    ys0s, xs0s = max(0, -dy), max(0, -dx)
    out[ys0:ys1, xs0:xs1] = mask[ys0s:ys0s + (ys1 - ys0), xs0s:xs0s + (xs1 - xs0)]
    return out


def _dilate(mask):
    """8-connected dilation by one pixel."""
    return (mask | _shift(mask, 1, 0) | _shift(mask, -1, 0) | _shift(mask, 0, 1) | _shift(mask, 0, -1)
            | _shift(mask, 1, 1) | _shift(mask, 1, -1) | _shift(mask, -1, 1) | _shift(mask, -1, -1))


def _flood_from_border(candidates):
    """Everything in `candidates` reachable from the image border (8-connected)."""
    cur = np.zeros_like(candidates)
    cur[0, :] = candidates[0, :]
    cur[-1, :] = candidates[-1, :]
    cur[:, 0] = candidates[:, 0]
    cur[:, -1] = candidates[:, -1]
    while True:
        nxt = _dilate(cur) & candidates
        if nxt.sum() == cur.sum():
            return nxt
        cur = nxt


def _background_color(rgb):
    """Median colour of the image's outer ring: the flat backdrop, auto-detected."""
    p = max(1, min(rgb.shape[:2]) // 100)
    ring = np.concatenate([
        rgb[:p, :, :].reshape(-1, 3),
        rgb[-p:, :, :].reshape(-1, 3),
        rgb[:, :p, :].reshape(-1, 3),
        rgb[:, -p:, :].reshape(-1, 3),
    ])
    return np.median(ring, axis=0)


def _dilate_n(mask, n):
    """Grow a boolean mask by n pixels (8-connected), via a fast max filter."""
    if n <= 0:
        return mask
    return np.asarray(
        Image.fromarray((mask * 255).astype(np.uint8), "L").filter(ImageFilter.MaxFilter(2 * n + 1))
    ) > 127


def knockout_alpha(rgb, barrier=55.0, edge=(6.0, 60.0), growth=3):
    """Alpha for art flattened onto a flat backdrop, with enclosed regions kept.

    Three ideas do the work:

    * **barrier** — the colour distance that counts as "clearly not backdrop".
      Everything past it forms the character's line art, which acts as a dam.
    * **reachability** — a region is backdrop only when the image border can
      reach it without crossing the barrier. Enclosed near-white areas (a face, a
      white shirt) therefore stay part of the subject instead of being punched
      out, which is the whole reason this is not a brightness threshold.
    * **rim** — only the band of subject pixels that actually touches the outside
      gets a coverage ramp. Everything deeper is fully opaque, so internal line
      art never picks up a transparent seam.

    `edge` is the (clear, opaque) colour-distance band: at or below `clear` a
    pixel is backdrop, at or above `opaque` it is solid character.
    """
    clear, opaque = float(edge[0]), float(edge[1])
    if opaque <= clear:
        raise SystemExit("spout: --edge needs its second value larger than the first")
    if barrier <= 0:
        raise SystemExit("spout: --barrier must be positive")

    bg = _background_color(rgb)
    dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))

    # Barrier, speckle-medianed, then grown: the growth is what stops the border
    # fill squeezing through a one-pixel gap in the line art.
    raw = dist > barrier
    raw = np.asarray(
        Image.fromarray((raw * 255).astype(np.uint8), "L").filter(ImageFilter.MedianFilter(5))
    ) > 127
    gate = _dilate_n(raw, growth)

    # Border flood: a cheap conservative pass at 1/4 scale finds the bulk of the
    # backdrop, then a full-resolution settle seeded from it fills thin channels.
    h, w = gate.shape
    k = 4 if min(h, w) >= 64 else 1
    if k > 1:
        small = gate[:h // k * k, :w // k * k].reshape(h // k, k, w // k, k).any(axis=(1, 3))
        seed = np.repeat(np.repeat(_flood_from_border(~small), k, axis=0), k, axis=1)
    else:
        seed = np.zeros_like(gate)
    candidates = ~gate
    filled = np.zeros_like(candidates)
    filled[:seed.shape[0], :seed.shape[1]] = seed
    filled &= candidates
    while True:
        nxt = _dilate(filled) & candidates
        if nxt.sum() == filled.sum():
            break
        filled = nxt

    silhouette = ~filled
    # The rim reaches `growth` px into the grown barrier plus 2 px of real
    # character, which is exactly the span that may be blended with the backdrop.
    rim = silhouette & _dilate_n(filled, growth + 2)
    coverage = np.clip((dist - clear) / (opaque - clear), 0.0, 1.0)

    alpha = np.where(silhouette, np.where(rim, coverage, 1.0), 0.0).astype(np.float32)
    alpha[alpha < 0.02] = 0.0
    return alpha


def _has_usable_alpha(image):
    """True when the file already carries a real alpha channel (not just opaque).

    Covers RGBA and palette images with a transparency index, so transparent
    GIFs/PNGs are passed through instead of being knocked out again.
    """
    try:
        alpha = np.asarray(image.convert("RGBA"))[..., 3]
    except (ValueError, OSError):
        return False
    return bool((alpha < 128).mean() > 0.001)


def load_pose_image(path, barrier=55.0, edge=(6.0, 60.0), force_knockout=False):
    """Load one pose as straight-alpha RGBA.

    Art that already has alpha is trusted as-is; flattened art gets the
    reachability-based knock-out. Returns (RGBA image, how alpha was obtained).
    """
    with Image.open(path) as raw:
        raw.load()
        has_alpha = _has_usable_alpha(raw)
        if has_alpha and not force_knockout:
            return raw.convert("RGBA"), "existing alpha"
        rgb = np.asarray(raw.convert("RGB"), dtype=np.float32)

    background = _background_color(rgb)
    alpha = knockout_alpha(rgb, barrier=barrier, edge=edge)
    return compose(rgb, alpha, background), "knocked out"


def compose(rgb, alpha, background):
    """Combine straight colour with alpha, un-premultiplying the backdrop blend.

    A pixel is the backdrop blend `C*a + bg*(1-a)`, so the true colour is
    recovered as `(P - bg*(1-a)) / a`; skipping this leaves a light halo wherever
    the silhouette is anti-aliased.
    """
    background = np.asarray(background, dtype=np.float32)
    safe = np.maximum(alpha, 1e-3)[..., None]
    color = np.clip((rgb - background * (1.0 - safe)) / safe, 0.0, 255.0)
    color = np.where(alpha[..., None] < 1e-3, background, color)
    blurred = np.asarray(
        Image.fromarray((alpha * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(0.6)),
        dtype=np.float32,
    ) / 255.0
    return Image.fromarray(np.dstack([color, blurred * 255.0]).astype(np.uint8), "RGBA")


def find_see_through(alpha):
    """Locate holes punched through a character, given a frame's alpha channel.

    Plain erosion is not enough here: eroding the opaque mask also erodes *away*
    from a hole, so a punched-out face never lands inside the eroded core. This
    reuses the reachability idea from the knock-out instead — a transparent region
    that cannot be reached from the image border is enclosed by the character, so
    it is a hole rather than legitimate backdrop.

    Returns (translucent pixel count inside the filled silhouette, silhouette size).
    """
    opaque = alpha > 128
    outside = _flood_from_border(~opaque)
    silhouette = ~outside
    core = np.asarray(
        Image.fromarray((silhouette * 255).astype(np.uint8), "L").filter(ImageFilter.MinFilter(11))
    ) > 127
    # 230 (0.9) rather than 250: the alpha blur leaves ~0.97 at the core's own
    # edge, which is opaque to the eye and must not read as a hole.
    return int((core & (alpha < 230)).sum()), int(silhouette.sum())


def subject_bbox(image, threshold=60):
    alpha = np.asarray(image)[..., 3]
    ys, xs = np.nonzero(alpha > threshold)
    if len(xs) == 0:
        raise ValueError("no subject pixels found after knock-out")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


# --------------------------------------------------------------------------- #
# Pose discovery
# --------------------------------------------------------------------------- #


def _stem(name):
    return os.path.splitext(name)[0].lower().replace("_", "-").replace(" ", "-")


def discover_poses(source, explicit=None):
    """Map contract states to image files found in `source` and `source/previews`."""
    explicit = explicit or {}
    candidates = []
    for root in (source, source / "previews"):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
                candidates.append(entry)

    by_state, ignored = {}, []
    for state in ROW_ORDER:
        if state in explicit:
            by_state[state] = explicit[state]
            continue
        match = None
        for alias in POSE_ALIASES[state]:
            for path in candidates:
                if _stem(path.name) == alias:
                    match = path
                    break
            if match:
                break
        if match is not None:
            by_state[state] = match

    taken = {p.resolve() for p in by_state.values()}
    ignored = [p for p in candidates if p.resolve() not in taken]
    return by_state, ignored


# --------------------------------------------------------------------------- #
# Canvas normalisation + motion
# --------------------------------------------------------------------------- #


def place_on_canvas(poses, boxes, canvas, pad):
    """Scale every pose by one shared factor and put it on a shared canvas.

    One global bounding box keeps the character's size and footing identical
    across states, so switching animation never makes the pet jump scale.
    """
    gx0 = min(b[0] for b in boxes.values())
    gy0 = min(b[1] for b in boxes.values())
    gx1 = max(b[2] for b in boxes.values())
    gy1 = max(b[3] for b in boxes.values())
    gw, gh = gx1 - gx0, gy1 - gy0
    avail_w, avail_h = canvas[0] - 2 * pad[0], canvas[1] - 2 * pad[1]
    scale = min(avail_w / gw, avail_h / gh)
    dw, dh = max(1, round(gw * scale)), max(1, round(gh * scale))

    placed = {}
    for state, image in poses.items():
        x0, y0, x1, y1 = boxes[state]
        crop = image.crop((x0, y0, x1, y1)).resize(
            (max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))), Image.LANCZOS
        )
        sheet = Image.new("RGBA", canvas, (255, 255, 255, 0))
        # Horizontally centred, feet on the bottom pad.
        sheet.alpha_composite(crop, ((canvas[0] - dw) // 2,
                                     (canvas[1] - pad[1] - dh) + round((y0 - gy0) * scale)))
        placed[state] = sheet
    return placed, scale, (gw, gh)


def transform(base, canvas, pad, dy=0.0, sx=1.0, sy=1.0, angle=0.0):
    """One output frame: rotate and scale about the footing, then offset."""
    out = base
    if angle:
        out = out.rotate(angle, resample=Image.BICUBIC, center=(canvas[0] / 2, canvas[1] - pad[1]))
    if sx != 1.0 or sy != 1.0:
        bw, bh = max(1, round(canvas[0] * sx)), max(1, round(canvas[1] * sy))
        scaled = out.resize((bw, bh), Image.LANCZOS)
        tmp = Image.new("RGBA", canvas, (255, 255, 255, 0))
        tmp.alpha_composite(scaled, ((canvas[0] - bw) // 2, canvas[1] - bh))
        out = tmp
    if dy:
        tmp = Image.new("RGBA", canvas, (255, 255, 255, 0))
        tmp.alpha_composite(out, (0, int(round(dy))))
        out = tmp
    return out


def resolve_motion(preset_name, poses, overrides=None):
    """Build the per-track frame plan from a preset plus optional JSON overrides."""
    key = PRESET_ALIASES.get(preset_name, preset_name)
    if key not in PRESETS:
        raise SystemExit(f"spout: unknown motion preset {preset_name!r}; "
                         f"choose from {', '.join(sorted(PRESETS))}")
    plan = {}
    for track, spec in PRESETS[key].items():
        pose = spec["pose"]
        if pose not in poses:
            raise SystemExit(f"spout: motion preset needs pose {pose!r} for track {track!r}")
        keys = [dict(k) for k in spec["keys"]]
        for k in keys:
            k.setdefault("ms", spec.get("ms", 200))
        plan[track] = {"pose": pose, "keys": keys}
    for track, spec in (overrides or {}).items():
        if track not in ROW_ORDER:
            raise SystemExit(f"spout: motion override names unknown track {track!r}")
        keys = spec.get("keys")
        if not isinstance(keys, list) or not keys:
            raise SystemExit(f"spout: motion override for {track!r} needs a non-empty 'keys' list")
        pose = spec.get("pose", plan[track]["pose"] if track in plan else track)
        if pose not in poses:
            raise SystemExit(f"spout: motion override for {track!r} needs pose {pose!r}")
        default_ms = spec.get("ms", 200)
        plan[track] = {"pose": pose, "keys": [{**{"ms": default_ms}, **dict(k)} for k in keys]}
    return plan


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_pet(source, *, pet_id, display_name, description="", license_id="", version="1.0.0",
              author="", canvas=(384, 480), pad=(10, 6), preset="default", motion_overrides=None,
              out_dir=None, barrier=55.0, edge=(6.0, 60.0), force_knockout=False, jobs=None,
              quality=86, preview_formats=("webp",), phases=None):
    """Run the whole hatch and return the built pet directory."""
    t0 = time.time()
    source = Path(source)
    if not source.is_dir():
        raise SystemExit(f"spout: source directory not found: {source}")
    if not ID_PATTERN.match(pet_id or ""):
        raise SystemExit(f"spout: id {pet_id!r} must be lowercase kebab-case")

    out_dir = Path(out_dir) if out_dir else Path.cwd() / "build" / pet_id
    explicit = {}
    config = source / "spout.json"
    if config.is_file():
        data = json.loads(config.read_text(encoding="utf-8"))
        explicit = {k: (source / v if not Path(v).is_absolute() else Path(v))
                    for k, v in (data.get("poses") or {}).items()}
        pet_id = data.get("id", pet_id)
        display_name = data.get("displayName", display_name)
        description = data.get("description", description)
        license_id = data.get("license", license_id)
        author = data.get("author", author) or author
        version = data.get("version", version)
        preset = data.get("motionPreset", preset)
        motion_overrides = {**(data.get("motion") or {}), **(motion_overrides or {})}
        phases = data.get("phases", phases)
        detail(f"read config {config}")

    log(f"spout {VERSION} — hatching {pet_id} ({display_name})")
    log(f"\n[1/7] discovering poses in {source}")
    found, ignored = discover_poses(source, explicit)
    for state in ROW_ORDER:
        if state in found:
            log(f"      {state:14s} <- {found[state].name}")
    missing = [s for s in ROW_ORDER if s not in found]
    if missing:
        raise SystemExit("spout: no image found for " + ", ".join(missing)
                         + "\n       name the files after the states (or add spout.json with a "
                           "'poses' map): " + ", ".join(ROW_ORDER))
    for path in ignored:
        detail(f"ignored (no state match): {path.name}")

    log(f"\n[2/7] knocking out the backdrop")
    jobs = jobs or min(4, os.cpu_count() or 1)
    poses, methods = {}, {}
    if jobs > 1 and len(found) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(found))) as pool:
            futures = {state: pool.submit(load_pose_image, str(path), barrier, edge, force_knockout)
                       for state, path in found.items()}
            for state, future in futures.items():
                poses[state], methods[state] = future.result()
    else:
        for state, path in found.items():
            poses[state], methods[state] = load_pose_image(path, barrier, edge, force_knockout)
    for state in ROW_ORDER:
        log(f"      {state:14s} {methods[state]}")

    log(f"\n[3/7] fitting a shared canvas {canvas[0]}x{canvas[1]}")
    boxes = {state: subject_bbox(image) for state, image in poses.items()}
    placed, scale, global_box = place_on_canvas(poses, boxes, canvas, pad)
    log(f"      global subject {global_box[0]}x{global_box[1]}, scale {scale:.4f}")

    log(f"\n[4/7] rendering frames (motion preset: {preset})")
    plan = resolve_motion(preset, placed, motion_overrides)
    frames_root = out_dir / "frames"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    encoded, manifest_tracks, total = {}, {}, 0
    for track in ROW_ORDER:
        spec = plan[track]
        target = frames_root / track
        target.mkdir(parents=True, exist_ok=True)
        names, durations = [], []
        for index, key in enumerate(spec["keys"], start=1):
            cache_key = (spec["pose"], round(float(key.get("dy", 0)), 2), round(float(key.get("sx", 1)), 4),
                         round(float(key.get("sy", 1)), 4), round(float(key.get("angle", 0)), 2))
            if cache_key not in encoded:
                from io import BytesIO
                buf = BytesIO()
                transform(placed[spec["pose"]], tuple(canvas), tuple(pad), **{
                    k: float(key.get(k, default)) for k, default in
                    (("dy", 0), ("sx", 1), ("sy", 1), ("angle", 0))
                }).save(buf, "WEBP", quality=quality, method=6, exact=True)
                encoded[cache_key] = buf.getvalue()
            name = f"{track}_{index:02d}.webp"
            (target / name).write_bytes(encoded[cache_key])
            names.append(name)
            ms = int(key.get("ms", 200))
            if not MIN_FRAME_MS <= ms <= MAX_FRAME_MS:
                raise SystemExit(f"spout: frameMs {ms} for {track} is outside "
                                 f"[{MIN_FRAME_MS}, {MAX_FRAME_MS}]")
            durations.append(ms)
            total += 1
        manifest_tracks[track] = {"frames": names, "frameMs": durations}
        size_kb = sum((target / n).stat().st_size for n in names) // 1024
        log(f"      {track:14s} {len(names)} frames  {size_kb} KB")
    log(f"      {total} frames, {len(encoded)} unique renders")

    log(f"\n[5/7] writing pet.json")
    manifest = {
        "petManifestVersion": 2,
        "id": pet_id,
        "displayName": display_name,
        "renderer": "frames2d",
        "license": license_id or "LicenseRef-All-Rights-Reserved",
        "frames2d": {
            "dir": "frames",
            "defaultFrameMs": 200,
            "tracks": manifest_tracks,
            "phases": dict(phases or PHASE_TRACK),
        },
    }
    if description:
        manifest["description"] = description
    if version:
        manifest["version"] = version
    if author:
        manifest["author"] = author
    (out_dir / "pet.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"      {(out_dir / 'pet.json')}")

    log(f"\n[6/7] building previews")
    previews = out_dir / "previews"
    previews.mkdir(parents=True, exist_ok=True)
    for track in ROW_ORDER:
        spec = manifest_tracks[track]
        images = [Image.open(frames_root / track / n).convert("RGBA") for n in spec["frames"]]
        flat = [im.convert("RGB") for im in images]
        for fmt in preview_formats:
            if fmt == "webp":
                flat[0].save(previews / f"{track}.webp", "WEBP", save_all=True,
                             append_images=flat[1:], duration=spec["frameMs"][0],
                             loop=0, quality=85, method=6)
            elif fmt == "gif":
                # GIF has no partial alpha: flatten onto a neutral plate first.
                plate = [Image.alpha_composite(Image.new("RGBA", im.size, (247, 248, 250, 255)), im).convert("P")
                         for im in images]
                plate[0].save(previews / f"{track}.gif", save_all=True, append_images=plate[1:],
                              duration=spec["frameMs"][0], loop=0, disposal=2)
    # Keep the untouched originals alongside, for provenance.
    originals = previews / "source"
    originals.mkdir(exist_ok=True)
    for state, path in found.items():
        shutil.copy2(path, originals / path.name)
    log(f"      {len(ROW_ORDER)} previews -> {previews}")

    log(f"\n[7/7] checking the result")
    report = audit(out_dir, expect_id=pet_id, plugin_check=False)
    if report["failures"]:
        for failure in report["failures"]:
            bad(failure)
        raise SystemExit("spout: refusing to ship a pet that failed its own checks")
    for label, extra in report["checks"]:
        ok(label, extra)

    log(f"\ndone in {time.time() - t0:.1f}s -> {out_dir}")
    log(f"next: spout install {out_dir} --select")
    return out_dir


# --------------------------------------------------------------------------- #
# Auditing
# --------------------------------------------------------------------------- #


def audit(pet_dir, expect_id=None, plugin_check=True, pet_id_hint=None):
    """Structural, alpha and (optionally) plugin-contract checks on a pet dir."""
    pet_dir = Path(pet_dir)
    checks, failures = [], []

    def check(label, passed, extra=""):
        checks.append((label, extra))
        if not passed:
            failures.append(label + (f" :: {extra}" if extra else ""))

    manifest_path = pet_dir / "pet.json"
    if not manifest_path.is_file():
        check("pet.json exists", False, str(manifest_path))
        return {"checks": checks, "failures": failures, "manifest": None}
    check("pet.json exists", True)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        check("pet.json parses", False, str(exc))
        return {"checks": checks, "failures": failures, "manifest": None}
    check("pet.json parses", True)

    known_top = {"$schema", "petManifestVersion", "id", "displayName", "description", "version",
                 "author", "license", "homepage", "renderer", "sprite2d", "live2d", "frames2d",
                 "sequences", "remarks"}
    extra_keys = set(manifest) - known_top
    check("no unknown top-level keys", not extra_keys, ", ".join(sorted(extra_keys)))
    check("manifest is v2", manifest.get("petManifestVersion") == 2)
    pet_id = manifest.get("id", "")
    check("id is lowercase kebab-case", bool(ID_PATTERN.match(pet_id)), pet_id)
    if expect_id:
        check("id matches the requested id", pet_id == expect_id, pet_id)
    check("license declared (v2 requires it)", bool(manifest.get("license")))
    check("displayName present", bool(manifest.get("displayName")))
    check("renderer is frames2d", manifest.get("renderer") == "frames2d")

    block = manifest.get("frames2d")
    check("frames2d block present", isinstance(block, dict))
    if not isinstance(block, dict):
        return {"checks": checks, "failures": failures, "manifest": manifest}

    check("no unknown frames2d keys", not (set(block) - {"dir", "defaultFrameMs", "tracks", "phases", "skins"}),
          ", ".join(sorted(set(block) - {"dir", "defaultFrameMs", "tracks", "phases", "skins"})))
    root = block.get("dir", ".")
    tracks = block.get("tracks") or {}
    phases = block.get("phases") or {}
    check("idle phase is mapped (frames2d requirement)", bool(phases.get("idle")),
          str(phases.get("idle")))
    unmapped = [p for p, t in phases.items() if t not in tracks]
    check("every phase maps to a declared track", not unmapped, ", ".join(unmapped))
    check("tracks declared (1..64)", 1 <= len(tracks) <= 64, str(len(tracks)))

    declared, problems = set(), []
    for track, spec in tracks.items():
        names = spec.get("frames") or []
        durations = spec.get("frameMs") or []
        if not names:
            problems.append(f"{track}: no frames")
            continue
        if len(durations) != len(names):
            problems.append(f"{track}: {len(durations)} durations for {len(names)} frames")
        if any(not isinstance(d, int) or not MIN_FRAME_MS <= d <= MAX_FRAME_MS for d in durations):
            problems.append(f"{track}: frameMs outside [{MIN_FRAME_MS}, {MAX_FRAME_MS}]")
        for name in names:
            rel = f"{root}/{track}/{name}" if root != "." else f"{track}/{name}"
            declared.add(rel)
            if not (pet_dir / rel).is_file():
                problems.append(f"{track}: missing frame {rel}")
    check("every declared frame exists with valid durations", not problems,
          "; ".join(problems[:4]))

    on_disk = set()
    if (pet_dir / root).is_dir():
        for track_dir in sorted((pet_dir / root).iterdir()):
            if track_dir.is_dir():
                for f in sorted(track_dir.iterdir()):
                    if f.is_file():
                        on_disk.add(f"{root}/{track_dir.name}/{f.name}")
    orphans = sorted(on_disk - declared)
    check("no orphan frame files", not orphans, ", ".join(orphans[:4]))

    # The regression guard that matters: a hole punched through the character
    # shows the GUI's dark chrome through its face.
    see_through, frames_checked, empties = [], 0, []
    for track in tracks:
        for name in tracks[track].get("frames") or []:
            rel = f"{root}/{track}/{name}" if root != "." else f"{track}/{name}"
            path = pet_dir / rel
            if not path.is_file():
                continue
            alpha = np.asarray(Image.open(path).convert("RGBA"))[..., 3]
            frames_checked += 1
            if not (alpha > 250).any():
                empties.append(rel)
                continue
            count, _size = find_see_through(alpha)
            if count:
                see_through.append((rel, count))
    check("every frame carries opaque pixels", not empties, ", ".join(empties[:3]))
    check("no holes punched through any character", not see_through,
          f"{len(see_through)} frame(s), e.g. {see_through[0][0]} ({see_through[0][1]} px)"
          if see_through else "")
    check("frames audited", frames_checked > 0, f"{frames_checked} frames")

    if plugin_check:
        plugin = find_plugin()
        node = find_node()
        if plugin is None or node is None:
            warn("plugin contract check skipped (needs @linxin666/dsh-pet and Node >= 22.6)")
        else:
            result = run_plugin_check(plugin, node, pet_dir, pet_id)
            found = result.get("found") is True
            check("plugin registry resolves the pet", found,
                  "" if found else (result.get("error") or "not registered; "
                                    + "; ".join(result.get("warnings") or [])[:160]))
            if found:
                errors = result.get("errors") or []
                check("plugin reports no error diagnostics", not errors, "; ".join(errors)[:200])
                renderer = result.get("renderer")
                check("plugin loaded a frames2d entry", renderer == "frames2d",
                      "" if renderer == "frames2d" else str(renderer))
                phases = result.get("phases") or {}
                check("plugin sees all 7 phases", len(phases) == 7,
                      "" if len(phases) == 7 else str(sorted(phases)))
    return {"checks": checks, "failures": failures, "manifest": manifest}


def find_plugin():
    """Locate an installed @linxin666/dsh-pet in the dsh profile."""
    candidates = []
    profile = os.environ.get("DSH_PROFILE_DIR")
    if profile:
        candidates.append(Path(profile) / "node_modules" / "@linxin666" / "dsh-pet")
    home = dsh_home()
    if home:
        candidates.append(home / "node_modules" / "@linxin666" / "dsh-pet")
        candidates += sorted((home / "profiles").glob("*/node_modules/@linxin666/dsh-pet"))
    for path in candidates:
        if (path / "src" / "registry.ts").is_file():
            return path
    return None


def find_node():
    """Locate a Node >= 22.6 (TS type stripping) for the plugin contract check."""
    found = shutil.which("node")
    if not found:
        home = dsh_home()
        if home:
            for pattern in ("dsh-runtimes/*/dependencies/node/bin/node",
                            "dsh-runtimes/*/dependencies/node/bin/node.exe"):
                hits = sorted(home.glob(pattern))
                if hits:
                    found = str(hits[-1])
                    break
    return found


def dsh_home():
    raw = os.environ.get("DSH_HOME")
    return Path(raw) if raw else None


PLUGIN_HARNESS = r"""
import { pathToFileURL } from 'node:url'
import { join } from 'node:path'
const [mirror, petsDir, id] = process.argv.slice(2)
const mod = await import(pathToFileURL(join(mirror, 'registry.ts')).href)
const registry = mod.loadPetRegistry({ packageRoot: mirror, dshPetsDir: petsDir })
const entry = registry.byId(id)
const errors = registry.diagnostics
  .filter(d => d.level === 'error' && String(d.source).includes(id))
  .map(d => d.message)
if (!entry) {
  console.log(JSON.stringify({ found: false, errors, warnings: registry.warnings.slice(0, 8) }))
  process.exit(0)
}
const tracks = {}
for (const [name, track] of Object.entries(entry.frames2d?.tracks ?? {})) tracks[name] = track.frames
console.log(JSON.stringify({
  found: true,
  renderer: entry.renderer,
  dir: entry.dir,
  phases: entry.frames2d?.phases ?? {},
  tracks,
  warnings: registry.warnings.filter(w => w.includes(id)),
  errors,
}))
"""


def run_plugin_check(plugin, node, pet_dir, pet_id):
    """Load the pet through the plugin's own registry code, outside node_modules.

    Node refuses to strip TypeScript under node_modules, so the plugin's sources
    are mirrored to a scratch directory first — the validator that runs is still
    the plugin's, byte for byte. The sources land at the mirror root so the
    module's own relative imports resolve unchanged.
    """
    pet_dir = Path(pet_dir).resolve()
    with tempfile.TemporaryDirectory(prefix="spout-plugin-") as tmp:
        tmp = Path(tmp)
        mirror = tmp / "dsh-pet"
        shutil.copytree(plugin / "src", mirror,
                        ignore=shutil.ignore_patterns("*.test.*", "__pycache__"))
        if (plugin / "assets").is_dir():
            shutil.copytree(plugin / "assets", mirror / "assets")
        harness = tmp / "check.mjs"
        harness.write_text(PLUGIN_HARNESS, encoding="utf-8")
        # The registry scans subdirectories of dshPetsDir; the pet's parent works
        # for both an installed pet and a freshly built staging directory.
        proc = subprocess.run([node, str(harness), str(mirror), str(pet_dir.parent), pet_id],
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout or "").strip().splitlines()
            return {"found": False, "error": "plugin check crashed: "
                    + (reason[-1] if reason else "unknown error")}
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"found": False, "error": "unparseable plugin output: " + proc.stdout.strip()[-200:]}


# --------------------------------------------------------------------------- #
# Install / select
# --------------------------------------------------------------------------- #


def install(pet_dir, dest=None, force=False, select=False):
    pet_dir = Path(pet_dir)
    manifest = json.loads((pet_dir / "pet.json").read_text(encoding="utf-8"))
    pet_id = manifest["id"]
    home = dsh_home()
    if home is None:
        raise SystemExit("spout: DSH_HOME is not set; pass --dest to choose a location")
    target = Path(dest) if dest else home / "pets" / pet_id
    if target.exists():
        if not force:
            raise SystemExit(f"spout: {target} already exists (use --force to replace it)")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(pet_dir, target)
    frames = sum(len(fs) for _, _, fs in os.walk(target / "frames"))
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    log(f"installed {pet_id} -> {target}")
    log(f"      {frames} frames, {size / 1024 / 1024:.2f} MB")

    if select:
        state_file = home / "pet.json"
        if state_file.is_file():
            backup = state_file.with_suffix(".json.before-" + pet_id)
            shutil.copy2(state_file, backup)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            state["petId"] = pet_id
            state.setdefault("names", {})[pet_id] = manifest.get("displayName", pet_id)
            state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                                  encoding="utf-8")
            log(f"      selection -> {pet_id} (backup: {backup})")
        else:
            warn(f"{state_file} not found; select {pet_id} from the pet settings card instead")
    log("\nthe registry is built once at plugin start — restart dsh web to pick the pet up")
    return target


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def cmd_states(args):
    found, ignored = discover_poses(Path(args.source))
    log(f"source: {Path(args.source).resolve()}")
    for state in ROW_ORDER:
        log(f"  {'OK ' if state in found else 'MISS'}  {state:14s} "
            + (str(found[state]) if state in found else "-"))
    for path in ignored:
        log(f"  --   (ignored)     {path}")
    missing = [s for s in ROW_ORDER if s not in found]
    if missing:
        log(f"\nmissing {len(missing)}: {', '.join(missing)}")
        log("rename those files after their state, or add a spout.json 'poses' map")
    return 0 if not missing else 1


def cmd_mask(args):
    image, how = load_pose_image(args.image, args.barrier, tuple(args.edge), args.force)
    out = Path(args.out)
    image.save(out)
    alpha = np.asarray(image)[..., 3]
    holes, silhouette = find_see_through(alpha)
    log(f"{how}: {out}  {image.size[0]}x{image.size[1]}  "
        f"opaque={int((alpha > 250).sum())}  holes-in-silhouette={holes}")
    if holes:
        bad("the knock-out punched holes through the character",
            f"{holes} px — lower --barrier or raise the opaque end of --edge")
        return 1
    if (alpha > 250).sum() < 0.005 * alpha.size:
        bad("almost nothing survived the knock-out", "raise --barrier")
        return 1
    ok("mask looks usable", f"silhouette {silhouette} px")
    return 0


def cmd_hatch(args):
    overrides = None
    if args.motion:
        overrides = json.loads(Path(args.motion).read_text(encoding="utf-8"))
        overrides = overrides.get("motion", overrides)
    build_pet(
        args.source,
        pet_id=args.id,
        display_name=args.name or args.id,
        description=args.description or "",
        license_id=args.license or "LicenseRef-All-Rights-Reserved",
        version=args.version,
        author=args.author or "",
        canvas=tuple(args.canvas),
        pad=tuple(args.pad),
        preset=args.motion_preset,
        motion_overrides=overrides,
        out_dir=args.out,
        barrier=args.barrier,
        edge=tuple(args.edge),
        force_knockout=args.force_knockout,
        jobs=args.jobs,
        quality=args.quality,
        preview_formats=[f.strip() for f in args.previews.split(",") if f.strip()],
    )
    return 0


def cmd_install(args):
    install(Path(args.pet_dir), dest=args.dest, force=args.force, select=args.select)
    return 0


def cmd_validate(args):
    global _VERBOSE
    _VERBOSE = args.verbose
    report = audit(Path(args.pet_dir), expect_id=args.expect_id,
                   plugin_check=not args.no_plugin_check)
    log(f"validating {Path(args.pet_dir).resolve()}")
    for label, extra in report["checks"]:
        ok(label, extra)
    log(f"\n{len(report['checks'])} checks, {len(report['failures'])} failed")
    for failure in report["failures"]:
        bad(failure)
    return 1 if report["failures"] else 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog=NAME, description=f"{NAME} {VERSION} — static pose stills into a living dsh-pet")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("states", help="show how pose files map onto the 9 contract states")
    p.add_argument("source")
    p.set_defaults(func=cmd_states)

    p = sub.add_parser("mask", help="knock the backdrop out of a single image")
    p.add_argument("image")
    p.add_argument("--out", required=True)
    p.add_argument("--barrier", type=float, default=55.0,
                   help="colour distance that counts as clearly non-backdrop (default 55)")
    p.add_argument("--edge", type=float, nargs=2, default=(6.0, 60.0), metavar=("CLEAR", "OPAQUE"),
                   help="colour-distance band: alpha 0 at CLEAR, 1 at OPAQUE (default 6 60)")
    p.add_argument("--force", action="store_true", help="ignore existing alpha and knock out anyway")
    p.set_defaults(func=cmd_mask)

    p = sub.add_parser("hatch", help="build a pet directory from a folder of pose stills")
    p.add_argument("source")
    p.add_argument("--id", required=True, help="pet id, lowercase kebab-case")
    p.add_argument("--name", help="display name (defaults to the id)")
    p.add_argument("--description")
    p.add_argument("--license", help="SPDX id or free text (v2 manifests require it)")
    p.add_argument("--author")
    p.add_argument("--version", default="1.0.0")
    p.add_argument("--out", help="output directory (default ./build/<id>)")
    p.add_argument("--canvas", type=int, nargs=2, default=(384, 480), metavar=("W", "H"))
    p.add_argument("--pad", type=int, nargs=2, default=(10, 6), metavar=("X", "Y"))
    p.add_argument("--motion-preset", default="default", help=f"{', '.join(sorted(PRESETS))} (default: default)")
    p.add_argument("--motion", help="JSON file overriding per-track motion keys")
    p.add_argument("--previews", default="webp", help="comma list of preview formats: webp,gif")
    p.add_argument("--quality", type=int, default=86, help="WebP quality (default 86)")
    p.add_argument("--barrier", type=float, default=55.0)
    p.add_argument("--edge", type=float, nargs=2, default=(6.0, 60.0), metavar=("CLEAR", "OPAQUE"))
    p.add_argument("--force-knockout", action="store_true",
                   help="knock out even when the files already carry alpha")
    p.add_argument("--jobs", type=int, help="parallel knock-out workers (default min(4, cpus))")
    p.set_defaults(func=cmd_hatch)

    p = sub.add_parser("install", help="copy a built pet into $DSH_HOME/pets/<id>/")
    p.add_argument("pet_dir")
    p.add_argument("--dest", help="install elsewhere (default $DSH_HOME/pets/<id>)")
    p.add_argument("--force", action="store_true", help="replace an existing install")
    p.add_argument("--select", action="store_true", help="also make it the active pet")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("validate", help="audit a pet directory")
    p.add_argument("pet_dir")
    p.add_argument("--expect-id")
    p.add_argument("--no-plugin-check", action="store_true",
                   help="skip loading the result through @linxin666/dsh-pet")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_validate)
    return parser


def main(argv=None):
    global _VERBOSE
    parser = build_parser()
    args = parser.parse_args(argv)
    _VERBOSE = getattr(args, "verbose", False)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        log("\nspout: interrupted")
        return 130
    except SystemExit as exc:
        if exc.code not in (0, None) and not isinstance(exc.code, str):
            return int(exc.code)
        raise


if __name__ == "__main__":
    sys.exit(main())
