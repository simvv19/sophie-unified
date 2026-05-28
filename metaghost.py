#!/usr/bin/env python3
"""
MetaGhost — Video uniqualizer for Reels/TikTok mass posting.

Generates N statistically unique variations of an input video by perturbing
9 fingerprint layers: metadata, color, geometry, edge, vignette, sharpness,
temporal, audio spectrum, audio dynamics, container.

Each variant is invisible to humans but distinct to platform detection
(pHash, dHash, Photo DNA, audio fingerprinting).

Usage:
    python3 metaghost.py input.mp4 -n 5 -o out/
    python3 metaghost.py input.mp4 -n 10 -o out/ --stealth 4
    python3 metaghost.py reel.mp4 -n 3 --account-prefix sophie

Stealth levels:
    1 = ultra-subtle (lowest detection bypass, lowest visual change)
    3 = balanced (default, recommended)
    5 = max paranoia (heaviest perturbations, slight visible difference)

Requirements:
    - ffmpeg (brew install ffmpeg)
    - Python 3.8+
"""
import argparse
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path


# =============================================================================
# RANDOMIZATION RANGES (stealth=3 baseline; scaled by stealth level)
# =============================================================================

RANGES = {
    # GEOMETRY
    "crop_pct":       (0.005, 0.025),
    "zoom":           (1.00, 1.04),
    "rotation_deg":   (-0.4, 0.4),
    "border_px":      [0, 1, 2, 3],
    # COLOR
    "brightness":     (-0.03, 0.03),
    "contrast":       (0.97, 1.03),
    "saturation":     (0.95, 1.05),
    "hue_deg":        (-3, 3),
    "gamma":          (0.95, 1.05),
    "gamma_r":        (0.97, 1.03),
    "gamma_g":        (0.97, 1.03),
    "gamma_b":        (0.97, 1.03),
    # SHARPNESS / GRAIN
    "noise":          (0, 6),
    "sharpen_amount": (-0.3, 0.3),     # negative = blur, positive = sharpen
    # VIGNETTE
    "vignette_angle": (1.4, 1.6),
    # TEMPORAL
    "speed":          (0.97, 1.03),
    "fps":            [29.97, 30, 30.03, 29.94],
    "trim_start_ms":  (0, 100),
    "trim_end_ms":    (0, 100),
    # AUDIO
    "audio_pitch":    (0.99, 1.01),
    "audio_volume":   (0.96, 1.04),
    "hf_noise_db":    (-50, -42),      # inaudible high-freq noise
    "eq_shelf_g":     (-1.5, 1.5),
    "eq_band_g":      (-2.0, 2.0),
    "eq_band_freq":   (200, 6000),
    # ENCODE
    "crf":            [20, 21, 22, 23, 24],
    "preset":         ["medium", "slow", "fast", "veryfast"],
    "bframes":        [2, 3, 4, 5],
    "refs":           [3, 4, 5],
}

FAKE_DEVICES = [
    ("Apple", "iPhone 15"),
    ("Apple", "iPhone 15 Plus"),
    ("Apple", "iPhone 15 Pro"),
    ("Apple", "iPhone 15 Pro Max"),
    ("Apple", "iPhone 16"),
    ("Apple", "iPhone 16 Plus"),
    ("Apple", "iPhone 16 Pro"),
    ("Apple", "iPhone 16 Pro Max"),
    ("Apple", "iPhone 16e"),
]
FAKE_SOFTWARE = [
    "iOS 17.4.1", "iOS 17.5", "iOS 17.5.1", "iOS 17.6", "iOS 17.6.1",
    "iOS 18.0", "iOS 18.0.1", "iOS 18.1", "iOS 18.1.1",
    "iOS 18.2", "iOS 18.3", "iOS 18.3.1", "iOS 18.4",
]


def amplify(rng, factor):
    if not isinstance(rng, tuple) or len(rng) != 2:
        return rng
    lo, hi = rng
    mid = (lo + hi) / 2
    return (mid - (mid - lo) * factor, mid + (hi - mid) * factor)


def pick(rng):
    if isinstance(rng, list):
        return random.choice(rng)
    if isinstance(rng, tuple):
        return random.uniform(*rng)
    return rng


def probe(path):
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    out = subprocess.check_output(cmd)
    return json.loads(out)


def build_deai_filter(intensity, is_video=True):
    """
    Phantom AI — QUALITY-FIRST anti-detection layer.
    Philosophy: invisible to human eye + IG quality classifier, but disrupts
    the patterns AI detectors look for (frequency analysis, smoothness metrics).

    Levels are calibrated to be SUB-JND (just-noticeable-difference) — even
    at level 3, the visual quality should be indistinguishable from source.

    What we do (all imperceptible):
      - Micro-grain on luma only (1-3 noise units, sub-JND)
      - Tiny warm shift (matches iPhone ISP — ALL real iPhones add this)
      - Sub-pixel chromatic aberration (1px max, like real lenses)
      - Tonal jiggle on midtones (breaks AI's "too perfect" tone curve)

    What we DON'T do anymore (kills quality / hurts IG reach):
      - No heavy grain (was 9-21, now max 4)
      - No blur (was visible at level 3, now zero)
      - No tmix (averages frames = looks soft)
      - No desaturation (IG penalizes flat colors)
      - No vignette / light leak (visible artifacts)
    """
    if intensity <= 0:
        return None
    f = []
    i = max(1, min(3, intensity))

    # 1. Micro-grain on LUMA only — invisible at full res, breaks smoothness
    # Real iPhone sensors have ~1-3 units of noise floor, so this is natural
    grain_y = 1 + i               # 2, 3, 4 (sub-JND threshold)
    grain_flag = "t+u" if is_video else "u"
    f.append(f"noise=c0s={grain_y}:c0f={grain_flag}")

    # 2. Tiny warm shift — every iPhone ISP does this naturally
    # Values < 0.015 are below human perception threshold
    rs = 0.005 + i * 0.003   # 0.008 → 0.014
    bs = -0.005 - i * 0.002  # -0.007 → -0.011
    f.append(f"colorbalance=rs={rs:.3f}:bs={bs:.3f}")

    # 3. Sub-pixel chromatic aberration — invisible, looks like real lens
    # Only at level 2+, max 1 pixel
    if i >= 2:
        f.append("rgbashift=rh=1:gh=0:bh=-1:rv=0:gv=0:bv=0")

    # 4. Tonal micro-jiggle — breaks AI's perfect tone curve without
    # changing perceived contrast. Each output value shifts by < 1%.
    # This is the "anti-AI" magic: AI classifiers fingerprint tone curves.
    if i >= 1:
        d = i * 0.003  # 0.003, 0.006, 0.009
        f.append(
            f"curves=master='0/0 0.25/{0.25-d:.4f} "
            f"0.5/{0.5+d:.4f} 0.75/{0.75-d:.4f} 1/1'"
        )

    # That's it. No blur, no tmix, no vignette, no heavy grain.
    # Quality preserved, AI patterns disrupted.
    return ",".join(f)


def build_video_filter(p):
    """
    Video filter chain. Tuned to be invisible to the eye while scrambling
    every detection fingerprint. No vignette, no curves, no aggressive color.
    Heavy lifting is done by encoding params + per-frame noise + chroma shift +
    metadata + temporal modulations (speed/fps).
    """
    filters = []

    # 0. De-AI cosmetic layer — runs FIRST so unicalization stacks on top
    deai_i = p.get("deai_intensity", 0)
    if deai_i > 0:
        deai_chain = build_deai_filter(deai_i, is_video=True)
        if deai_chain:
            filters.append(deai_chain)

    # 1. Subtle crop (0.5-2.5% borders)
    cp = p["crop_pct"] * 0.5
    filters.append(f"crop=iw*(1-{cp*2}):ih*(1-{cp*2})")

    # 2. Light zoom (max 2%)
    z = 1 + (p["zoom"] - 1) * 0.5
    filters.append(f"scale=iw*{z}:ih*{z}")
    filters.append("crop=floor(iw/2)*2:floor(ih/2)*2")

    # 3. Tiny rotation (max 0.2°) — rotate INSIDE original dims, then crop the
    # black corner triangles, then scale back up. No more dark corners.
    rot = p["rotation_deg"] * 0.5 * 3.14159265 / 180
    if abs(rot) > 0.001:
        filters.append(f"rotate={rot}:fillcolor=black")
        # Crop ~1.5% off each side to remove black triangles, then upscale
        filters.append("crop=iw*0.97:ih*0.97")
        filters.append("scale=iw/0.97:ih/0.97")
        filters.append("crop=floor(iw/2)*2:floor(ih/2)*2")

    # 4. Color eq — halved magnitude so changes stay imperceptible
    filters.append(
        f"eq=brightness={p['brightness']*0.5:.4f}:"
        f"contrast={1+(p['contrast']-1)*0.5:.4f}:"
        f"saturation={1+(p['saturation']-1)*0.5:.4f}:"
        f"gamma={1+(p['gamma']-1)*0.5:.4f}:"
        f"gamma_r={1+(p['gamma_r']-1)*0.5:.4f}:"
        f"gamma_g={1+(p['gamma_g']-1)*0.5:.4f}:"
        f"gamma_b={1+(p['gamma_b']-1)*0.5:.4f}"
    )
    filters.append(f"hue=h={p['hue_deg']*0.5:.2f}")

    # 5. NO curves (visible)
    # 6. NO vignette (very visible)
    # 7. NO sharpen/blur by default

    # 8. Per-channel temporal noise — each frame has unique pixel pattern.
    #    Capped to invisible level (3) but enough to scramble pHash chains.
    n_y = min(3, int(p["noise"]))
    n_u = max(1, int(n_y * 0.6))
    n_v = max(1, int(n_y * 0.6))
    if n_y > 0:
        filters.append(
            f"noise=c0s={n_y}:c0f=t+u:c1s={n_u}:c1f=t+u:c2s={n_v}:c2f=t+u"
        )

    # 9. Sub-pixel RGB chroma shift (1 px max)
    if p["chroma_shift"]:
        rh = random.choice([-1, 0, 1])
        rv = random.choice([-1, 0, 1])
        gh = random.choice([-1, 0, 1])
        gv = random.choice([-1, 0, 1])
        bh = random.choice([-1, 0, 1])
        bv = random.choice([-1, 0, 1])
        if (rh, rv, gh, gv, bh, bv) != (0, 0, 0, 0, 0, 0):
            filters.append(
                f"rgbashift=rh={rh}:rv={rv}:gh={gh}:gv={gv}:bh={bh}:bv={bv}"
            )

    # 10. Per-pixel deterministic LSB perturbation (sub-JND)
    if p["geq_perturb"]:
        sx = random.randint(1, 17)
        sy = random.randint(1, 17)
        filters.append(
            f"geq=lum='clip(lum(X,Y)+mod(X*{sx}+Y*{sy}+N,3)-1,0,255)':"
            f"cb='cb(X,Y)':cr='cr(X,Y)'"
        )

    # 11. Edge replication (1-2 px)
    if p["border_px"] > 0:
        b = min(2, p["border_px"])
        filters.append(f"pad=iw+{b*2}:ih+{b*2}:{b}:{b}:color=black")
        filters.append(f"crop=iw-{b*2}:ih-{b*2}")

    # 12. Mirror flip (50/50 if enabled)
    if p["mirror"]:
        filters.append("hflip")

    # 13. FPS conversion (variant ≈30)
    filters.append(f"fps={p['fps']}")

    # 14. Speed (±3% PTS rescale)
    filters.append(f"setpts=PTS/{p['speed']:.4f}")

    # 15. Even-dim guard
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    return ",".join(filters)


def build_audio_filter(p):
    """Multi-band audio fingerprint scrambling."""
    filters = []
    speed = p["speed"]
    pitch = p["audio_pitch"]

    # Match audio tempo to video speed (atempo accepts 0.5-100)
    speed_safe = max(0.5, min(2.0, speed))
    filters.append(f"atempo={speed_safe:.4f}")

    # Pitch shift via sample rate trick — clamp pitch to safe range
    pitch_safe = max(0.85, min(1.15, pitch))
    if abs(pitch_safe - 1.0) > 0.0001:
        filters.append(
            f"asetrate=44100*{pitch_safe:.4f},aresample=44100,atempo={1/pitch_safe:.4f}"
        )

    # Random parametric EQ band (mid frequencies)
    # Clamp to valid range — amplify() can push it negative at high stealth
    eq_freq = max(80, min(8000, p['eq_band_freq']))
    filters.append(
        f"equalizer=f={eq_freq:.0f}:t=q:w=1:g={p['eq_band_g']:.2f}"
    )

    # High-shelf shift (treble)
    filters.append(f"highshelf=g={p['eq_shelf_g']:.2f}:f=8000")

    # Low-shelf shift (bass) — additional spectrum scrambling
    filters.append(
        f"lowshelf=g={random.uniform(-1.0, 1.0):.2f}:f={random.choice([100, 150, 200])}"
    )

    # Slow stereo amplitude modulation (sub-audible LFO ~0.1 Hz)
    # Disrupts audio fingerprint without being audible
    filters.append(
        f"apulsator=hz={random.uniform(0.05, 0.15):.3f}:"
        f"offset_l=0:offset_r=0:"
        f"width={random.uniform(0.005, 0.02):.4f}"
    )

    # Final volume
    filters.append(f"volume={p['audio_volume']:.4f}")

    return ",".join(filters)


def gen_variant(input_path, output_path, params, info):
    duration = float(info["format"]["duration"])
    ts = max(0.0, params["trim_start_ms"] / 1000)
    te = max(0.0, params["trim_end_ms"] / 1000)
    new_dur = max(0.5, duration - ts - te)

    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])

    vf = build_video_filter(params)
    af = build_audio_filter(params) if has_audio else None

    make, model = random.choice(FAKE_DEVICES)
    software = random.choice(FAKE_SOFTWARE)
    fake_date = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(time.time() - random.randint(60, 30 * 86400)),
    )
    title = "IMG_{:04d}".format(random.randint(1, 9999))

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-ss", f"{ts}",
        "-i", str(input_path),
        "-t", f"{new_dur}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", params["preset"],
        "-crf", str(params["crf"]),
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", random.choice(["4.0", "4.1", "4.2"]),
        "-g", str(random.choice([30, 48, 60, 90])),
        "-keyint_min", str(random.choice([15, 24, 30])),
        "-bf", str(params["bframes"]),
        "-refs", str(params["refs"]),
    ]

    if af:
        cmd += [
            "-af", af,
            "-c:a", "aac",
            "-b:a", random.choice(["128k", "160k", "192k"]),
            "-ar", "44100",
        ]
    else:
        cmd += ["-an"]

    cmd += [
        "-map_metadata", "-1",
        "-metadata", f"title={title}",
        "-metadata", f"creation_time={fake_date}Z",
        "-metadata", f"make={make}",
        "-metadata", f"model={model}",
        "-metadata", f"software={software}",
        "-metadata", f"comment=IMG{random.randint(1000, 9999)}",
        "-movflags", "+faststart",
        str(output_path),
    ]

    subprocess.run(cmd, check=True)


def build_image_filter(p):
    """
    Image-only filter chain. Tuned to be MUCH more conservative than video
    because still photos have no temporal masking — every artifact is visible.
    Strategy: invisible perceptual changes + heavy metadata/encoding scrambling.
    """
    filters = []

    # 0. De-AI cosmetic layer
    deai_i = p.get("deai_intensity", 0)
    if deai_i > 0:
        deai_chain = build_deai_filter(deai_i, is_video=False)
        if deai_chain:
            filters.append(deai_chain)

    # 1. Subtle crop (0.3-1% of borders, half what video uses)
    cp = p["crop_pct"] * 0.4
    filters.append(f"crop=iw*(1-{cp*2}):ih*(1-{cp*2})")

    # 2. Light zoom (max 2% vs 4% on video)
    z = 1 + (p["zoom"] - 1) * 0.5
    filters.append(f"scale=iw*{z}:ih*{z}")
    filters.append("crop=floor(iw/2)*2:floor(ih/2)*2")

    # 3. Tiny rotation — same fix: rotate in-place + crop black corners
    rot = p["rotation_deg"] * 0.5 * 3.14159265 / 180
    if abs(rot) > 0.001:
        filters.append(f"rotate={rot}:fillcolor=black")
        filters.append("crop=iw*0.97:ih*0.97")
        filters.append("scale=iw/0.97:ih/0.97")
        filters.append("crop=floor(iw/2)*2:floor(ih/2)*2")

    # 4. Color eq — keep but reduce brightness shifts
    filters.append(
        f"eq=brightness={p['brightness']*0.5:.4f}:"
        f"contrast={1+(p['contrast']-1)*0.5:.4f}:"
        f"saturation={1+(p['saturation']-1)*0.5:.4f}:"
        f"gamma={1+(p['gamma']-1)*0.5:.4f}:"
        f"gamma_r={1+(p['gamma_r']-1)*0.5:.4f}:"
        f"gamma_g={1+(p['gamma_g']-1)*0.5:.4f}:"
        f"gamma_b={1+(p['gamma_b']-1)*0.5:.4f}"
    )
    filters.append(f"hue=h={p['hue_deg']*0.5:.2f}")

    # 5. NO curves (too visible on still images)
    # 6. NO vignette (way too visible on still images)
    # 7. NO sharpen/blur by default (only at very high stealth)

    # 8. Light spatial noise (cap at 3 vs 6 on video)
    n = min(3, int(p["noise"]))
    if n > 0:
        filters.append(f"noise=c0s={n}:c0f=u:c1s={max(1,n-1)}:c1f=u:c2s={max(1,n-1)}:c2f=u")

    # 9. RGB chroma shift — invisible 1-2 px shifts (kills aligned pHash)
    if p["chroma_shift"]:
        rh = random.choice([-1, 0, 1])
        rv = random.choice([-1, 0, 1])
        gh = random.choice([-1, 0, 1])
        gv = random.choice([-1, 0, 1])
        bh = random.choice([-1, 0, 1])
        bv = random.choice([-1, 0, 1])
        if (rh, rv, gh, gv, bh, bv) != (0, 0, 0, 0, 0, 0):
            filters.append(f"rgbashift=rh={rh}:rv={rv}:gh={gh}:gv={gv}:bh={bh}:bv={bv}")

    # 10. Geq LSB perturbation — sub-JND (invisible to eye)
    if p["geq_perturb"]:
        sx = random.randint(1, 17)
        sy = random.randint(1, 17)
        filters.append(
            f"geq=lum='clip(lum(X,Y)+mod(X*{sx}+Y*{sy},3)-1,0,255)':"
            f"cb='cb(X,Y)':cr='cr(X,Y)'"
        )

    # 11. Edge replication (1-2 px, invisible)
    if p["border_px"] > 0:
        b = min(2, p["border_px"])
        filters.append(f"pad=iw+{b*2}:ih+{b*2}:{b}:{b}:color=black")
        filters.append(f"crop=iw-{b*2}:ih-{b*2}")

    # 12. Mirror (still 50/50 if enabled)
    if p["mirror"]:
        filters.append("hflip")

    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    return ",".join(filters)


def gen_variant_image(input_path, output_path, params):
    """Encode one uniqualized image variant. Output is JPEG."""
    make, model = random.choice(FAKE_DEVICES)
    software = random.choice(FAKE_SOFTWARE)
    fake_date = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(time.time() - random.randint(60, 30 * 86400)),
    )
    title = "IMG_{:04d}".format(random.randint(1, 9999))

    vf = build_image_filter(params)
    # JPEG quality varies (qscale 2-6 ≈ 92-78% quality, all real-world iPhone-ish)
    qscale = random.choice([2, 3, 3, 4, 4, 5])

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-i", str(input_path),
        "-vf", vf,
        "-frames:v", "1",
        "-update", "1",
        "-q:v", str(qscale),
        "-map_metadata", "-1",
        "-metadata", f"title={title}",
        "-metadata", f"creation_time={fake_date}Z",
        "-metadata", f"make={make}",
        "-metadata", f"model={model}",
        "-metadata", f"software={software}",
        "-metadata", f"comment=IMG{random.randint(1000, 9999)}",
        str(output_path),
    ]
    subprocess.run(cmd, check=True)


def is_image(path):
    suf = Path(path).suffix.lower()
    return suf in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def make_params(stealth=3):
    """Sample one set of randomized params, scaled by stealth level."""
    factor = 0.5 + (stealth - 1) * 0.4   # 1=>0.5, 3=>1.3, 5=>2.1
    p = {}
    for k, v in RANGES.items():
        if isinstance(v, tuple):
            p[k] = pick(amplify(v, factor))
        else:
            p[k] = pick(v)
    p["mirror"] = random.random() < 0.5
    p["vignette"] = random.random() < (0.4 + stealth * 0.1)
    p["chroma_shift"] = random.random() < (0.5 + stealth * 0.1)
    p["geq_perturb"] = random.random() < (0.3 + stealth * 0.15)
    return p


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", help="Input video file")
    ap.add_argument("-n", "--count", type=int, default=5)
    ap.add_argument("-o", "--output", default="out")
    ap.add_argument("--stealth", type=int, default=3, choices=[1, 2, 3, 4, 5])
    ap.add_argument("--no-mirror", action="store_true")
    ap.add_argument("--account-prefix", default="acc")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found. Install: brew install ffmpeg")

    inp = Path(args.input)
    if not inp.exists():
        sys.exit(f"File not found: {inp}")

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    info = probe(inp)
    print(f"Input: {inp.name} ({info['format']['duration']}s)")
    print(f"Stealth: {args.stealth}/5  |  Variants: {args.count}  |  → {out}/")
    print()

    for i in range(1, args.count + 1):
        p = make_params(stealth=args.stealth)
        if args.no_mirror:
            p["mirror"] = False

        out_file = out / f"{args.account_prefix}_{i:02d}_{inp.stem}.mp4"
        t0 = time.time()
        try:
            gen_variant(inp, out_file, p, info)
            size_kb = out_file.stat().st_size / 1024
            tag = "↔" if p["mirror"] else " "
            vt = "◐" if p["vignette"] else " "
            print(
                f"  [{i:02d}/{args.count:02d}] {out_file.name}  "
                f"{size_kb:>6.0f}KB  {time.time()-t0:>4.1f}s  "
                f"{tag}{vt}  spd={p['speed']:.3f}  crf={p['crf']}"
            )
        except subprocess.CalledProcessError as e:
            print(f"  [{i:02d}/{args.count:02d}] FAILED: {e}", file=sys.stderr)

    print(f"\n✓ Done → {out}/")


if __name__ == "__main__":
    main()
