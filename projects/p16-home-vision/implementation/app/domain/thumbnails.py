from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from app.domain.media_access import resolve_media_path


THUMBNAIL_ROOT = "recordings/thumbnails"
SOURCE_THUMBNAIL_ROOT = "thumbnail-cache/source"
THUMBNAIL_TIMEOUT_SECONDS = 120
THUMBNAIL_STREAM_MAX_BYTES = 16 * 1024**2
THUMBNAIL_STREAM_TIMEOUT_SECONDS = 30
# Keep clear of common HEVC decoder warm-up frames, then fall back for short clips.
THUMBNAIL_SEEKS = ("10", "5", "2", "1", "0.5", "0")


class ThumbnailError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def thumbnail_relative_path(asset_id: str) -> str:
    return f"{THUMBNAIL_ROOT}/{asset_id}.jpg"


def thumbnail_path(volume_root: Path, asset_id: str) -> Path:
    return resolve_media_path(volume_root, thumbnail_relative_path(asset_id))


def source_thumbnail_path(cache_root: Path, source_recording_id: str, locator_fingerprint: str | None) -> Path:
    key = f"{source_recording_id}-{(locator_fingerprint or 'unknown')[:32]}"
    return resolve_media_path(cache_root, f"{SOURCE_THUMBNAIL_ROOT}/{key}.jpg")


def _stats_are_near_uniform_gray(output: str) -> bool:
    values: dict[str, float] = {}
    for line in output.splitlines():
        key, separator, value = line.rpartition("=")
        if separator and key.startswith("lavfi.signalstats."):
            try:
                values[key.rsplit(".", 1)[-1]] = float(value)
            except ValueError:
                continue
    return not values or values.get("YHIGH", 0) - values.get("YLOW", 999) <= 3 and values.get("SATAVG", 999) <= 1


def thumbnail_is_usable(path: Path) -> bool:
    """Return true only for a decodable image with visible variation."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mjpeg", "-i", str(path),
                "-vf", "signalstats,metadata=print:file=-", "-frames:v", "1", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=THUMBNAIL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return not _stats_are_near_uniform_gray(result.stdout + result.stderr)


def generate_thumbnail(
    source_path: Path,
    output_path: Path,
    *,
    overwrite: bool = False,
    reject_uniform: bool = False,
) -> str:
    """Create one durable sidecar atomically; preserve good sidecars by default."""
    if output_path.is_file() and output_path.stat().st_size > 0 and not overwrite:
        return "already_present"
    if not source_path.is_file():
        raise ThumbnailError("THUMBNAIL_SOURCE_MISSING")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        part_path.unlink()
    except FileNotFoundError:
        pass
    last_error: BaseException | None = None
    for seek in THUMBNAIL_SEEKS:
        try:
            filters = "scale=640:-2"
            if reject_uniform:
                filters += ",signalstats,metadata=print:file=/dev/stderr"
            result = subprocess.run(
                [
                    "nice", "-n", "19", "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
                    "-i", str(source_path), "-ss", seek, "-frames:v", "1", "-vf", filters,
                    "-pix_fmt", "yuvj420p", "-q:v", "5", "-f", "mjpeg", str(part_path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=THUMBNAIL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            result = None
        if result is not None and result.returncode == 0 and part_path.is_file() and part_path.stat().st_size > 0:
            if reject_uniform:
                try:
                    if _stats_are_near_uniform_gray(result.stderr):
                        part_path.unlink()
                        continue
                except (OSError, subprocess.SubprocessError) as exc:
                    last_error = exc
                    try:
                        part_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
            os.replace(part_path, output_path)
            return "created"
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
    raise ThumbnailError("THUMBNAIL_FFMPEG_FAILED") from last_error


def generate_thumbnail_from_stream(
    chunks: Iterable[bytes],
    output_path: Path,
    *,
    seek_seconds: float,
    timeout_seconds: int = THUMBNAIL_STREAM_TIMEOUT_SECONDS,
    overwrite: bool = False,
) -> str:
    """Extract one frame from a bounded NVR stream without saving video."""
    if output_path.is_file() and output_path.stat().st_size > 0 and not overwrite:
        return "already_present"
    if seek_seconds < 0 or timeout_seconds < 1:
        raise ValueError("THUMBNAIL_STREAM_OPTIONS_INVALID")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        part_path.unlink()
    except FileNotFoundError:
        pass

    command = [
        "nice", "-n", "19", "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-i", "pipe:0", "-ss", f"{seek_seconds:g}", "-frames:v", "1",
        "-vf", "scale=640:-2", "-pix_fmt", "yuvj420p", "-q:v", "5",
        "-f", "mjpeg", str(part_path),
    ]
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = time.monotonic()
        for chunk in chunks:
            if time.monotonic() - started > timeout_seconds:
                raise ThumbnailError("THUMBNAIL_STREAM_TIMEOUT")
            if process.poll() is not None:
                break
            try:
                process.stdin.write(chunk)
            except BrokenPipeError:
                break
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.wait(timeout=max(1, timeout_seconds - int(time.monotonic() - started)))
        if process.returncode == 0 and part_path.is_file() and thumbnail_is_usable(part_path):
            os.replace(part_path, output_path)
            return "created"
    except (OSError, subprocess.SubprocessError) as exc:
        error = exc
    except ThumbnailError as exc:
        error = exc
    else:
        error = None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
    raise ThumbnailError(getattr(error, "code", "THUMBNAIL_STREAM_FFMPEG_FAILED")) from error
