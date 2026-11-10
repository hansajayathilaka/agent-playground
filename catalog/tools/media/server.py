# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "pillow>=10"]
# ///
"""media-tools: a small MCP server for photo and video edits.

Run with `uv run --script server.py` (uv installs the deps on first run).
Image tools use Pillow; video tools shell out to ffmpeg/ffprobe, which must be
on PATH. Paths are resolved against the server's working directory, which is
the agent folder when Claude Code starts it from the agent's `.mcp.json`.
Every tool writes a new file and returns its path; inputs are never changed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

mcp = FastMCP("media-tools")


def _in(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"{path} is not a file")
    return p


def _out(path: str | None, source: Path, suffix: str, ext: str | None = None) -> Path:
    if path:
        out = Path(path).expanduser()
    else:
        out = source.with_name(f"{source.stem}{suffix}{ext or source.suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _save(img: Image.Image, out: Path) -> str:
    if out.suffix.lower() in {".jpg", ".jpeg"} and img.mode not in {"RGB", "L"}:
        img = img.convert("RGB")
    img.save(out)
    return str(out)


# ------------------------------------------------------------------ images


@mcp.tool()
def image_info(path: str) -> dict:
    """Size, mode and format of an image."""
    with Image.open(_in(path)) as img:
        return {"width": img.width, "height": img.height, "mode": img.mode, "format": img.format}


@mcp.tool()
def image_resize(path: str, width: int | None = None, height: int | None = None,
                 output: str | None = None) -> str:
    """Resize an image. Give width, height or both; one alone keeps the aspect ratio."""
    src = _in(path)
    with Image.open(src) as img:
        if width and height:
            size = (width, height)
        elif width:
            size = (width, round(img.height * width / img.width))
        elif height:
            size = (round(img.width * height / img.height), height)
        else:
            raise ValueError("give width or height")
        return _save(img.resize(size, Image.Resampling.LANCZOS), _out(output, src, f"-{size[0]}x{size[1]}"))


@mcp.tool()
def image_crop(path: str, left: int, top: int, right: int, bottom: int, output: str | None = None) -> str:
    """Crop an image to the box (left, top, right, bottom) in pixels."""
    src = _in(path)
    with Image.open(src) as img:
        return _save(img.crop((left, top, right, bottom)), _out(output, src, "-crop"))


@mcp.tool()
def image_convert(path: str, format: str, output: str | None = None) -> str:
    """Convert an image to another format (png, jpg, webp, ...)."""
    src = _in(path)
    ext = "." + format.lower().lstrip(".")
    with Image.open(src) as img:
        return _save(img, _out(output, src, "", ext))


@mcp.tool()
def image_adjust(path: str, brightness: float = 1.0, contrast: float = 1.0, saturation: float = 1.0,
                 sharpness: float = 1.0, rotate: float = 0, grayscale: bool = False,
                 blur_radius: float = 0, autocontrast: bool = False, output: str | None = None) -> str:
    """Adjust an image. Factors are 1.0 for unchanged (0.5 halves, 1.5 adds half); rotate is degrees counter-clockwise."""
    src = _in(path)
    with Image.open(src) as original:
        img = ImageOps.exif_transpose(original)
        if autocontrast:
            img = ImageOps.autocontrast(img.convert("RGB"))
        for enhancer, factor in ((ImageEnhance.Brightness, brightness), (ImageEnhance.Contrast, contrast),
                                 (ImageEnhance.Color, saturation), (ImageEnhance.Sharpness, sharpness)):
            if factor != 1.0:
                img = enhancer(img).enhance(factor)
        if grayscale:
            img = ImageOps.grayscale(img)
        if blur_radius:
            img = img.filter(ImageFilter.GaussianBlur(blur_radius))
        if rotate:
            img = img.rotate(rotate, expand=True)
        return _save(img, _out(output, src, "-edit"))


# ------------------------------------------------------------------- video


def _ffmpeg(*args: str) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not on PATH")
    done = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(done.stderr.strip()[-500:] or f"ffmpeg exited {done.returncode}")


@mcp.tool()
def video_info(path: str) -> dict:
    """Duration, streams, codecs and resolution of a video (ffprobe)."""
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("ffprobe is not on PATH")
    done = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                           str(_in(path))], capture_output=True, text=True, check=True)
    data = json.loads(done.stdout)
    streams = [{k: s.get(k) for k in ("codec_type", "codec_name", "width", "height", "r_frame_rate", "channels")
                if s.get(k) is not None} for s in data.get("streams", [])]
    fmt = data.get("format", {})
    return {"duration_s": float(fmt.get("duration", 0)), "size_bytes": int(fmt.get("size", 0)), "streams": streams}


@mcp.tool()
def video_trim(path: str, start: str, end: str, output: str | None = None) -> str:
    """Cut a clip from start to end (seconds or HH:MM:SS). Re-encodes for frame-accurate cuts."""
    src = _in(path)
    out = _out(output, src, "-trim")
    _ffmpeg("-i", str(src), "-ss", start, "-to", end, "-c:v", "libx264", "-c:a", "aac", str(out))
    return str(out)


@mcp.tool()
def video_convert(path: str, format: str = "mp4", width: int | None = None, output: str | None = None) -> str:
    """Convert a video to another container (mp4, webm, mov, gif), optionally scaling to a width."""
    src = _in(path)
    out = _out(output, src, "-conv", "." + format.lower().lstrip("."))
    args = ["-i", str(src)]
    if width:
        args += ["-vf", f"scale={width}:-2"]
    _ffmpeg(*args, str(out))
    return str(out)


@mcp.tool()
def video_frames(path: str, every_seconds: float = 1.0, output_dir: str | None = None) -> str:
    """Extract one PNG frame every N seconds into a folder; returns the folder."""
    src = _in(path)
    folder = Path(output_dir).expanduser() if output_dir else src.with_name(f"{src.stem}-frames")
    folder.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-i", str(src), "-vf", f"fps=1/{every_seconds}", str(folder / "frame-%04d.png"))
    return str(folder)


@mcp.tool()
def video_extract_audio(path: str, format: str = "mp3", output: str | None = None) -> str:
    """Extract the audio track of a video (mp3, wav, m4a)."""
    src = _in(path)
    out = _out(output, src, "", "." + format.lower().lstrip("."))
    _ffmpeg("-i", str(src), "-vn", str(out))
    return str(out)


if __name__ == "__main__":
    mcp.run()
