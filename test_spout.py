#!/usr/bin/env python3
"""spout self-test.

Covers the paths that matter and, above all, the checks that must *fail*: a pet
with a hole punched through its character, a missing frame, an orphan frame and an
invalid id all have to be rejected. Run with:

    python test_spout.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
import spout  # noqa: E402  (also provides the reachability helper the test asserts with)

PY = sys.executable
SPOUT = str(Path(__file__).parent / "spout.py")
WORK = Path(__file__).parent / "build" / "selftest"
STATES = ["idle", "running-right", "running-left", "waving", "jumping",
          "failed", "waiting", "running", "review"]

failures = []


def run(*args):
    proc = subprocess.run([PY, SPOUT, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def expect(label, condition, extra=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  ({extra})" if extra and not condition else ""))
    if not condition:
        failures.append(label)


def make_source(root, mode):
    """Nine poses. 'flat' paints them on white; 'alpha' gives them real alpha.

    The shape is the point: a near-white body inside a dark outline, which is
    exactly what a brightness-only knock-out punches holes through.
    """
    root.mkdir(parents=True, exist_ok=True)
    for i, state in enumerate(STATES):
        size = 240 + i * 8
        # RGBA needs a 4-tuple: a 3-tuple keeps alpha at 255 and would make the
        # "already has alpha" source opaque.
        base = (255, 255, 255) if mode == "flat" else (0, 0, 0, 0)
        im = Image.new("RGB" if mode == "flat" else "RGBA", (size, size), base)
        d = ImageDraw.Draw(im)
        stroke = max(2, size // 60)
        d.ellipse([size * 0.25, size * 0.12, size * 0.75, size * 0.88],
                  fill=(250, 250, 252), outline=(40, 60, 120), width=stroke)
        d.rectangle([size * 0.30, size * 0.55, size * 0.70, size * 0.86],
                    fill=(252, 252, 255), outline=(40, 60, 120), width=stroke)
        im.save(root / f"{state}.png")


if WORK.exists():
    shutil.rmtree(WORK)

print("=== 1. flat backdrop (knock-out path), default motion ===")
src = WORK / "flat-src"
make_source(src, "flat")
rc, out = run("hatch", str(src), "--id", "self-flat", "--name", "自测·平板",
              "--license", "CC0-1.0", "--out", str(WORK / "flat-pet"))
expect("hatch succeeded", rc == 0, out[-500:])
expect("knock-out path used", "knocked out" in out)
expect("all 9 tracks rendered", out.count(" frames ") >= 9)
rc, out = run("validate", str(WORK / "flat-pet"), "--expect-id", "self-flat")
expect("validate passes", rc == 0, out[-800:])
expect("hole check reported as a PASS", "PASS  no holes punched through any character" in out)

print("\n=== 2. source already carrying alpha (pass-through path), still preset ===")
src2 = WORK / "alpha-src"
make_source(src2, "alpha")
rc, out = run("hatch", str(src2), "--id", "self-alpha", "--license", "CC0-1.0",
              "--motion-preset", "still", "--out", str(WORK / "alpha-pet"))
expect("hatch succeeded", rc == 0, out[-500:])
expect("existing alpha was trusted", "existing alpha" in out)
manifest = json.loads((WORK / "alpha-pet" / "pet.json").read_text(encoding="utf-8"))
counts = {t: len(s["frames"]) for t, s in manifest["frames2d"]["tracks"].items()}
expect("still preset = 1 frame per track", set(counts.values()) == {1}, str(counts))
rc, out = run("validate", str(WORK / "alpha-pet"), "--expect-id", "self-alpha")
expect("validate passes", rc == 0, out[-800:])

print("\n=== 3. the guard must catch a transparent interior (the reported bug) ===")
broken = WORK / "broken-pet"
shutil.copytree(WORK / "flat-pet", broken)
target = broken / "frames" / "idle" / "idle_01.webp"
arr = np.asarray(Image.open(target).convert("RGBA")).copy()
opaque = arr[..., 3] > 128
# Pick the hole site from the frame itself: erode the silhouette hard, then zero a
# box at its centre, so the hole is guaranteed to be enclosed by the character
# rather than a notch cut out of its edge.
inner = np.asarray(
    Image.fromarray((opaque * 255).astype(np.uint8), "L").filter(ImageFilter.MinFilter(41))
) > 127
ys, xs = np.nonzero(inner)
cy, cx = int(np.median(ys)), int(np.median(xs))
rows, cols = slice(cy - 45, cy + 45), slice(cx - 35, cx + 35)
arr[rows, cols, 3] = 0
Image.fromarray(arr, "RGBA").save(target)

check = np.asarray(Image.open(target).convert("RGBA"))[..., 3]
expect("injected hole is transparent", bool((check[rows, cols] == 0).all()))
expect("injected hole is enclosed (unreachable from the border)",
       int(spout._flood_from_border(~(check > 128))[rows, cols].sum()) == 0)
rc, out = run("validate", str(broken), "--expect-id", "self-flat", "--no-plugin-check")
expect("validate FAILS on the injected hole", rc != 0)
expect("failure line names the hole check",
       "FAIL  no holes punched through any character" in out, out[-600:])

print("\n=== 4. a missing frame must fail structurally ===")
broken2 = WORK / "missing-pet"
shutil.copytree(WORK / "flat-pet", broken2)
(broken2 / "frames" / "idle" / "idle_02.webp").unlink()
rc, out = run("validate", str(broken2), "--expect-id", "self-flat", "--no-plugin-check")
expect("validate FAILS on a missing frame", rc != 0)
expect("failure line names the missing frame",
       "FAIL  every declared frame exists" in out, out[-600:])

print("\n=== 5. an orphan frame must fail ===")
broken3 = WORK / "orphan-pet"
shutil.copytree(WORK / "flat-pet", broken3)
shutil.copy2(broken3 / "frames" / "idle" / "idle_01.webp",
             broken3 / "frames" / "idle" / "idle_99.webp")
rc, out = run("validate", str(broken3), "--expect-id", "self-flat", "--no-plugin-check")
expect("validate FAILS on an orphan frame", rc != 0)
expect("failure line names the orphan", "FAIL  no orphan frame files" in out, out[-600:])

print("\n=== 6. an invalid id must be rejected ===")
rc, out = run("hatch", str(src), "--id", "Not Kebab", "--license", "CC0-1.0",
              "--out", str(WORK / "bad-pet"))
expect("uppercase/space id rejected", rc != 0 and "kebab" in out)

print("\n=== 7. pose discovery must report missing states ===")
rc, out = run("states", str(WORK / "empty-src"))
expect("empty source exits non-zero", rc != 0, out[-200:])

print("\n" + "=" * 46)
print(f"self-test failures: {len(failures)}")
for f in failures:
    print("  FAILED:", f)
sys.exit(1 if failures else 0)
