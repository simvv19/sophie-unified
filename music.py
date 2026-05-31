#!/usr/bin/env python3
"""
Music overlay module — replace video audio with an MP3.

Uses ffmpeg to drop the original audio track (if any) and mux the MP3
audio onto the video, cut to video length (no fade).
"""
import subprocess
from pathlib import Path

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"


def apply_music_to_video(input_video, mp3_path, output_video, volume=1.0):
    """Replace video audio with MP3, cut to video length."""
    # Build audio filter: volume scaling, then aac encode
    af = f"volume={volume}" if abs(volume - 1.0) > 0.01 else "anull"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_video),
        "-i", str(mp3_path),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-af", af,
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_video),
    ]
    subprocess.run(cmd, check=True)


AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".aac", ".mp4", ".mov", ".m4v")

def list_library(library_root):
    """Return {folder_name: [audio_filenames]} for the music library."""
    root = Path(library_root)
    if not root.exists():
        return {}
    result = {}
    root_files = sorted([f.name for f in root.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS])
    if root_files:
        result["default"] = root_files
    for d in sorted(root.iterdir()):
        if d.is_dir():
            files = sorted([f.name for f in d.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS])
            if files:
                result[d.name] = files
    return result
