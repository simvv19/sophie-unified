#!/usr/bin/env python3
"""
Text overlay module — renders Instagram-style text on videos.

Uses PIL to render a transparent PNG with text using IG fonts, then
ffmpeg to composite the overlay onto each video.
"""
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path.home() / "Library/Fonts"
SYS_FONTS = Path("/System/Library/Fonts")

# Mapping style key → (font file path, ttc index, variation name or None, default tracking px)
STYLE_FONTS_MAP = {
    "modern_regular":(FONTS_DIR / "IG_Optimistic_VF.ttf", 0, "Regular 95", -5),   # IG "Modern" Regular ⭐
    "modern_medium": (FONTS_DIR / "IG_Optimistic_VF.ttf", 0, "Medium 95", -3),
    "modern":        (FONTS_DIR / "IG_Optimistic_Display_Bold.ttf", 0, None, -2),
    "ios_bold":      (SYS_FONTS / "Helvetica.ttc", 1, None, 0),
    "strong":        (FONTS_DIR / "IG_Facebook_Narrow.ttf", 0, None, 0),
    "classic":       (FONTS_DIR / "IG_UI_Beta_Bold.ttf", 0, None, 0),
    "neon":          (FONTS_DIR / "CosmopolitanScriptRegular.otf", 0, None, 0),
    "aveny":         (FONTS_DIR / "AvenyTMedium.otf", 0, None, 0),
}

# Backward-compat: existing code reads STYLE_FONTS expecting just paths
STYLE_FONTS = {k: v[0] for k, v in STYLE_FONTS_MAP.items()}


def _probe_size(video_path):
    """Returns (w, h) of the video."""
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0",
        str(video_path),
    ]).decode().strip().split(",")
    return int(out[0]), int(out[1])


def _measure(draw, line, font, tracking):
    """Width of line with per-char tracking added."""
    if tracking == 0:
        return draw.textbbox((0, 0), line, font=font)[2]
    total = 0
    for ch in line:
        bb = draw.textbbox((0, 0), ch, font=font)
        total += (bb[2] - bb[0]) + tracking
    return total - tracking if line else 0


def _draw_tracked(draw, x, y, line, font, fill, tracking):
    """Draw line char-by-char with extra tracking px between glyphs."""
    if tracking == 0:
        draw.text((x, y), line, font=font, fill=fill)
        return
    cx = x
    for ch in line:
        draw.text((cx, y), ch, font=font, fill=fill)
        bb = draw.textbbox((0, 0), ch, font=font)
        cx += (bb[2] - bb[0]) + tracking


def _wrap(draw, text, font, max_w, tracking=0):
    """Word-wrap text to fit max_w pixels."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if _measure(draw, test, font, tracking) > max_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def render_overlay_png(out_png, W, H, text, style="modern", position="top",
                       color="white", bg="none", size_pct=0.052,
                       max_width_pct=0.86):
    """
    Render the text overlay as a transparent PNG of size W×H.
    style: modern / modern_medium / strong / classic / neon / aveny
    position: top / middle / bottom
    color: 'white' or 'black' or '#RRGGBB'
    bg: 'none' or 'pill_black' or 'pill_white' (background behind text)
    """
    entry = STYLE_FONTS_MAP.get(style)
    if not entry:
        raise FileNotFoundError(f"Unknown style: {style}")
    font_path, idx, variation, def_tracking = entry
    if not font_path.exists():
        raise FileNotFoundError(f"Font '{style}' not installed: {font_path}")

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fs = int(H * size_pct)
    font = ImageFont.truetype(str(font_path), fs, index=idx)
    if variation:
        try:
            font.set_variation_by_name(variation.encode() if isinstance(variation, str) else variation)
        except Exception:
            pass
    # Scale default tracking proportionally to font size (defined at fs~63)
    tracking = int(def_tracking * (fs / 63.0)) if def_tracking else 0
    max_w = int(W * max_width_pct)
    lines = _wrap(draw, text, font, max_w, tracking=tracking)

    line_h = int(fs * 1.18)
    block_h = line_h * len(lines)
    if position == "top":
        y0 = int(H * 0.10)
    elif position == "bottom":
        y0 = int(H * 0.78) - block_h
    else:
        y0 = (H - block_h) // 2

    # Text color
    if color == "white":
        rgb = (255, 255, 255)
    elif color == "black":
        rgb = (10, 10, 10)
    elif color.startswith("#") and len(color) == 7:
        rgb = (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
    else:
        rgb = (255, 255, 255)

    # Padding presets per bg type
    if bg in ("highlight_black", "highlight_white"):
        # Tight hug, like Reels highlight
        pad_x, pad_y = int(fs * 0.30), int(fs * 0.10)
        radius = int(fs * 0.18)
    elif bg in ("pill_black", "pill_white"):
        pad_x, pad_y = int(fs * 0.55), int(fs * 0.30)
        radius = int(fs * 0.55)
    else:
        pad_x, pad_y = 0, 0
        radius = 0

    for i, line in enumerate(lines):
        tw = _measure(draw, line, font, tracking)
        x = (W - tw) // 2
        y = y0 + i * line_h

        # Background (per-line hug)
        bg_fill = None
        if bg == "pill_black":          bg_fill = (0, 0, 0, 150)
        elif bg == "pill_white":        bg_fill = (255, 255, 255, 240)
        elif bg == "highlight_black":   bg_fill = (0, 0, 0, 255)
        elif bg == "highlight_white":   bg_fill = (255, 255, 255, 255)
        if bg_fill:
            rect = [x - pad_x, y - pad_y, x + tw + pad_x, y + fs + pad_y]
            if radius > 0:
                draw.rounded_rectangle(rect, radius=radius, fill=bg_fill)
            else:
                draw.rectangle(rect, fill=bg_fill)

        # Soft drop shadow (only when no bg)
        if bg == "none":
            sh = max(1, fs // 18)
            for dx, dy, a in [(sh + 1, sh + 1, 60), (sh, sh, 110), (sh - 1, sh - 1, 130)]:
                _draw_tracked(draw, x + dx, y + dy, line, font, (0, 0, 0, a), tracking)

        # Stroke (black outline around each letter — CapCut/Edits style)
        if bg in ("stroke_black", "stroke_white"):
            stroke_color = (0, 0, 0, 255) if bg == "stroke_black" else (255, 255, 255, 255)
            stroke_w = max(2, int(fs * 0.06))
            for dx in range(-stroke_w, stroke_w + 1):
                for dy in range(-stroke_w, stroke_w + 1):
                    if dx * dx + dy * dy <= stroke_w * stroke_w:
                        _draw_tracked(draw, x + dx, y + dy, line, font, stroke_color, tracking)

        # Main text (no faux-bold — preserve thin weight)
        _draw_tracked(draw, x, y, line, font, rgb + (255,), tracking)

    img.save(out_png)


def apply_to_video(input_video, output_video, text, style="modern",
                   position="top", color="white", bg="none", size_pct=0.052,
                   workdir=None):
    """Composite text overlay onto a video. Output as .mp4."""
    W, H = _probe_size(input_video)
    overlay_png = Path(workdir or "/tmp") / f"_overlay_{Path(input_video).stem}.png"
    render_overlay_png(overlay_png, W, H, text, style=style, position=position,
                       color=color, bg=bg, size_pct=size_pct)

    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_video), "-i", str(overlay_png),
        "-filter_complex", "[0:v][1:v]overlay=0:0",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(output_video),
    ], check=True)

    try:
        overlay_png.unlink()
    except OSError:
        pass


def apply_to_image(input_image, output_image, text, style="modern",
                   position="top", color="white", bg="none", size_pct=0.052):
    """Composite text overlay onto an image (JPG/PNG)."""
    base = Image.open(input_image).convert("RGBA")
    W, H = base.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # Render directly onto a tmp overlay then composite
    tmp_path = Path(output_image).parent / f"_tmp_overlay_{Path(input_image).stem}.png"
    render_overlay_png(tmp_path, W, H, text, style=style, position=position,
                       color=color, bg=bg, size_pct=size_pct)
    over = Image.open(tmp_path).convert("RGBA")
    composed = Image.alpha_composite(base, over)
    composed.convert("RGB").save(output_image, quality=92)
    try:
        tmp_path.unlink()
    except OSError:
        pass


def available_styles():
    """Returns list of styles whose fonts are actually installed."""
    return [k for k, p in STYLE_FONTS.items() if p.exists()]
