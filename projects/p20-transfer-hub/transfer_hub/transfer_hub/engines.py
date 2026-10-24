from __future__ import annotations

import json
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

Progress = Callable[[float | None, int | None, int | None], None]
ProcessStarted = Callable[[int], None]
ProcessEnded = Callable[[], None]


class EngineError(RuntimeError):
    pass


def available_tools() -> dict[str, bool]:
    return {
        "aria2": shutil.which("aria2c") is not None,
        "yt-dlp": shutil.which("yt-dlp") is not None,
        "gallery-dl": shutil.which("gallery-dl") is not None,
        "nm3u8dl": shutil.which("N_m3u8DL-RE") is not None,
        "uav": Path(os.getenv("P20_UAV_SOURCE", "/opt/p20-uav/src")).is_dir(),
        "7z": shutil.which("7z") is not None or shutil.which("7zz") is not None,
    }


def _run(
    argv: list[str],
    cancel: Callable[[], bool],
    progress: Progress | None = None,
    process_started: ProcessStarted | None = None,
    process_ended: ProcessEnded | None = None,
    message: Callable[[str], None] | None = None,
) -> list[str]:
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env={**os.environ, "LC_ALL": "C"},
        start_new_session=True,
    )
    if process_started:
        process_started(proc.pid)
    assert proc.stdout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    pending = b""
    output_tail: deque[str] = deque(maxlen=12)
    last_message = 0.0
    message_count = 0
    protocol_output = "transfer_hub.uav_runner" in argv

    def handle_line(raw: bytes) -> None:
        nonlocal last_message, message_count
        line = raw.decode(errors="replace").strip()
        if line.startswith("P20_UAV "):
            data = json.loads(line[8:])
            if data.get("type") == "progress" and progress:
                progress(data["percent"], data["speed"], data["eta"])
            elif data.get("type") == "message" and message:
                message(data["message"][:500])
            return
        if line:
            safe = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", line)
            safe = re.sub(r"(?i)(authorization|cookie):\s*[^\s]+", r"\1: <redacted>", safe)
            output_tail.append(safe[:500])
            now = time.monotonic()
            if message and "下載中:" not in line and "合成影片中" not in line and message_count < 400 and now - last_message >= 1:
                message(safe[:500])
                last_message = now
                message_count += 1
        if not progress or protocol_output:
            return
        pct = None
        m = re.search(r"(\d{1,3}(?:\.\d+)?)%", line)
        if m:
            pct = min(100.0, float(m.group(1)))
        progress(pct, None, None)

    try:
        while True:
            if cancel():
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                raise EngineError("cancelled")

            if proc.poll() is not None:
                tail = proc.stdout.read()
                if tail:
                    pending += tail
                break

            for key, _mask in selector.select(timeout=0.2):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    continue
                pending += chunk
                while re.search(rb"[\r\n]", pending):
                    line, pending = re.split(rb"[\r\n]", pending, maxsplit=1)
                    handle_line(line)
                if len(pending) > 65536:
                    handle_line(pending[:65536])
                    pending = b""

        for line in re.split(rb"[\r\n]", pending):
            if line:
                handle_line(line)
        rc = proc.wait()
        if rc != 0:
            detail = " | ".join(output_tail)
            message = f"command exited with status {rc}: {Path(argv[0]).name}"
            if detail:
                message += f" :: {detail}"
            raise EngineError(message)
        return list(output_tail)
    finally:
        selector.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if process_ended:
            process_ended()



def _run_password_prompt(
    argv: list[str],
    password: str,
    cancel: Callable[[], bool],
) -> list[str]:
    """Run a password-prompting CLI on a real controlling PTY.

    7-Zip reads passwords from /dev/tty for some operations. ``pty.fork`` gives
    the child a controlling terminal while keeping the password out of argv.
    """
    if os.name != "posix":
        raise EngineError("secure 7-Zip password prompting requires a POSIX host")
    if "\n" in password or "\r" in password:
        raise EngineError("archive password contains unsupported line breaks")
    import pty
    import select

    pid, master = pty.fork()
    if pid == 0:
        env = {**os.environ, "LC_ALL": "C"}
        os.execvpe(argv[0], argv, env)

    output = bytearray()
    sent_prompts = 0
    prompt_re = re.compile(rb"(?:Enter|Reenter) password[^:\r\n]*:", re.I)
    status = None
    exit_seen_at = None
    try:
        while True:
            if cancel():
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                raise EngineError("cancelled")

            ready, _, _ = select.select([master], [], [], 0.2)
            eof = False
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                    eof = True
                if chunk:
                    output.extend(chunk)
                    prompts = len(prompt_re.findall(output))
                    while sent_prompts < prompts:
                        os.write(master, (password + "\n").encode())
                        sent_prompts += 1
                else:
                    eof = True

            if status is None:
                waited, child_status = os.waitpid(pid, os.WNOHANG)
                if waited == pid:
                    status = child_status
                    exit_seen_at = time.monotonic()
            if status is not None:
                if eof or (not ready and exit_seen_at is not None and time.monotonic() - exit_seen_at >= 0.2):
                    break

        rc = os.waitstatus_to_exitcode(status)
        decoded = output.decode(errors="replace")
        safe_lines = [line.strip()[:500] for line in re.split(r"[\r\n]+", decoded) if line.strip()]
        if rc != 0:
            detail = " | ".join(safe_lines[-8:])
            message = f"command exited with status {rc}: {Path(argv[0]).name}"
            if detail:
                message += f" :: {detail}"
            raise EngineError(message)
        return safe_lines[-12:]
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        if status is None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def download(
    engine: str,
    source: str,
    out_dir: Path,
    filename: str,
    headers: dict[str, str],
    cancel: Callable[[], bool],
    progress: Progress | None = None,
    process_started: ProcessStarted | None = None,
    process_ended: ProcessEnded | None = None,
    engine_options: dict | None = None,
    message: Callable[[str], None] | None = None,
) -> Path | list[Path]:
    engine_options = engine_options or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    if engine == "uav":
        from .uav import recognizes
        if not recognizes(source):
            raise EngineError("unsupported UAV source")
        if not available_tools()["uav"]:
            raise EngineError("UAV is not installed")
        target_name = Path(filename).stem + ".mp4"
        config_file = out_dir / ".uav-job.json"
        config_file.touch(mode=0o600, exist_ok=False)
        config_file.write_text(json.dumps({"source": source, "out_dir": str(out_dir.resolve()), "filename": target_name, "headers": headers}))
        try:
            _run([sys.executable, "-u", "-m", "transfer_hub.uav_runner", str(config_file)], cancel, progress, process_started, process_ended, message)
        finally:
            config_file.unlink(missing_ok=True)
        artifact = out_dir / target_name
        if artifact.is_symlink() or not artifact.is_file() or not artifact.stat().st_size:
            raise EngineError("UAV produced no validated video")
        return artifact

    if engine == "aria2":
        exe = shutil.which("aria2c")
        if not exe:
            raise EngineError("aria2c is not installed")
        argv = [
            exe,
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--summary-interval=1",
            "--console-log-level=notice",
            "-d", str(out_dir),
            "-o", filename,
        ]
        for key, value in headers.items():
            argv.append(f"--header={key}: {value}")
        argv.append(source)
        _run(argv, cancel, progress, process_started, process_ended)
        return out_dir / filename

    if engine == "yt-dlp":
        exe = shutil.which("yt-dlp")
        if not exe:
            raise EngineError("yt-dlp is not installed")
        template = str(out_dir / (Path(filename).stem + "-%(playlist_index|1)s.%(ext)s"))
        argv = [exe, "--newline"]
        if not engine_options.get("allow_playlist"):
            argv.append("--no-playlist")
        argv += ["-o", template]
        for key, value in headers.items():
            argv += ["--add-header", f"{key}:{value}"]
        argv.append(source)
        _run(argv, cancel, progress)
        matches = sorted(out_dir.glob(Path(filename).stem + "-*.*"), key=lambda p: p.name)
        if not matches:
            raise EngineError("yt-dlp produced no file")
        return matches if len(matches) > 1 else matches[0]

    if engine == "gallery-dl":
        exe = shutil.which("gallery-dl")
        if not exe:
            raise EngineError("gallery-dl is not installed")
        before = {p.resolve() for p in out_dir.rglob("*") if p.is_file()}
        argv = [exe, "--no-mtime", "-D", str(out_dir)]
        if engine_options.get("twitter_images_only"):
            argv += ["-o", "extractor.twitter.videos=false"]
            argv += ["-o", "extractor.twitter.size=orig"]
        if fmt := engine_options.get("filename_format"):
            argv += ["-f", str(fmt)]
        for key, value in headers.items():
            argv += ["--header", f"{key}: {value}"]
        argv.append(source)
        _run(argv, cancel, progress)
        after = [p for p in out_dir.rglob("*") if p.is_file() and p.resolve() not in before]
        if not after:
            raise EngineError("gallery-dl produced no file")
        return sorted(after, key=lambda p: p.name)

    if engine == "nm3u8dl":
        exe = shutil.which("N_m3u8DL-RE")
        if not exe:
            raise EngineError("N_m3u8DL-RE is not installed")
        stem = Path(filename).stem
        argv = [exe, source, "--save-dir", str(out_dir), "--save-name", stem, "--auto-select", "--use-ffmpeg-concat-demuxer"]
        for key, value in headers.items():
            argv += ["--header", f"{key}: {value}"]
        output_tail = _run(argv, cancel, progress)
        matches = sorted(out_dir.glob(stem + "*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not matches:
            detail = " | ".join(output_tail[-6:])
            raise EngineError("N_m3u8DL-RE produced no file" + (f" :: {detail}" if detail else ""))
        return matches[0]

    raise EngineError(f"unsupported engine: {engine}")


def verify_7z_password(archive: Path, password: str, cancel: Callable[[], bool]) -> None:
    exe = shutil.which("7z") or shutil.which("7zz")
    if not exe:
        raise EngineError("7z/7zz is not installed")
    _run_password_prompt([exe, "t", str(archive)], password, cancel)


def pack_zip_aes(
    source: Path,
    output: Path,
    password: str,
    volume_size: str | None,
    cancel: Callable[[], bool],
) -> list[Path]:
    """Create an AES-256 encrypted ZIP for desktop delivery."""
    exe = shutil.which("7z") or shutil.which("7zz")
    if not exe:
        raise EngineError("7z/7zz is not installed")
    argv = [exe, "a", "-tzip", "-mx=1", "-mem=AES256", "-p", str(output), str(source)]
    if volume_size:
        if not re.fullmatch(r"\d+[kKmMgG]?", volume_size):
            raise EngineError("invalid ZIP volume size")
        argv += [f"-v{volume_size}"]
    _run(argv, cancel)
    if volume_size:
        return sorted(output.parent.glob(output.name + ".*"))
    return [output]


def pack_zip_compat(
    source: Path,
    output: Path,
    password: str | None,
    volume_size: str | None,
    cancel: Callable[[], bool],
) -> list[Path]:
    """Create a macOS Archive Utility compatible ZIP.

    Password protection intentionally uses legacy ZipCrypto because Apple's built-in
    Archive Utility does not reliably support AES-encrypted ZIP/7z archives. This is
    a compatibility mode, not the strong-encryption mode.
    """
    exe = shutil.which("7z") or shutil.which("7zz")
    if not exe:
        raise EngineError("7z/7zz is not installed")
    if volume_size:
        raise EngineError("split volumes are not supported in macOS ZIP compatibility mode")
    argv = [exe, "a", "-tzip", "-mx=1", str(output), str(source)]
    if password:
        argv += ["-p", "-mem=ZipCrypto"]
        _run_password_prompt(argv, password, cancel)
    else:
        _run(argv, cancel)
    return [output]


def pack_zip_aes(
    source: Path,
    output: Path,
    password: str,
    volume_size: str | None,
    cancel: Callable[[], bool],
) -> list[Path]:
    """Create a WinZip-compatible AES-256 encrypted ZIP.

    ZIP is the cross-platform container. AES-256 replaces legacy ZipCrypto.
    A compatible archive app is still required on platforms whose built-in
    file manager does not implement encrypted ZIP extensions.
    """
    exe = shutil.which("7z") or shutil.which("7zz")
    if not exe:
        raise EngineError("7z/7zz is not installed")
    argv = [exe, "a", "-tzip", "-mx=1", "-mem=AES256", "-p", str(output), str(source)]
    if volume_size:
        if not re.fullmatch(r"\d+[kKmMgG]?", volume_size):
            raise EngineError("invalid ZIP volume size")
        argv += [f"-v{volume_size}"]
    _run_password_prompt(argv, password, cancel)
    if volume_size:
        return sorted(output.parent.glob(output.name + ".*"))
    return [output]



def pack_7z(
    source: Path,
    output: Path,
    password: str | None,
    volume_size: str | None,
    cancel: Callable[[], bool],
) -> list[Path]:
    exe = shutil.which("7z") or shutil.which("7zz")
    if not exe:
        raise EngineError("7z/7zz is not installed")
    argv = [exe, "a", "-t7z", "-mx=3", str(output), str(source)]
    if password:
        argv += ["-p", "-mhe=on"]
    if volume_size:
        if not re.fullmatch(r"\d+[kKmMgG]?", volume_size):
            raise EngineError("invalid 7z volume size")
        argv += [f"-v{volume_size}"]
    if password:
        _run_password_prompt(argv, password, cancel)
    else:
        _run(argv, cancel)
    if volume_size:
        return sorted(output.parent.glob(output.name + ".*"))
    return [output]
