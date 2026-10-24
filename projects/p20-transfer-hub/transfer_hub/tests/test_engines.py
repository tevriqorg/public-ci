import os
import signal
import sys
import threading
import time
from pathlib import Path

from transfer_hub.engines import _run


def test_process_group_can_pause_and_resume(tmp_path: Path):
    output = tmp_path / "ticks.txt"
    script = tmp_path / "slow_writer.py"
    script.write_text(
        "import pathlib, time\n"
        f"p = pathlib.Path({str(output)!r})\n"
        "for i in range(20):\n"
        "    with p.open('a') as f:\n"
        "        f.write(str(i) + '\\n')\n"
        "        f.flush()\n"
        "    time.sleep(0.08)\n"
    )

    started = threading.Event()
    pgid_box = {}

    def on_started(pgid: int):
        pgid_box["pgid"] = pgid
        started.set()

    errors = []

    def runner():
        try:
            _run(
                [sys.executable, "-u", str(script)],
                lambda: False,
                process_started=on_started,
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    assert started.wait(2)

    deadline = time.time() + 2
    while time.time() < deadline:
        if output.exists() and len(output.read_text().splitlines()) >= 3:
            break
        time.sleep(0.02)

    assert output.exists()
    os.killpg(pgid_box["pgid"], signal.SIGSTOP)
    before = len(output.read_text().splitlines())
    time.sleep(0.35)
    during = len(output.read_text().splitlines())
    assert during == before

    os.killpg(pgid_box["pgid"], signal.SIGCONT)
    thread.join(4)
    assert not thread.is_alive()
    assert not errors
    assert len(output.read_text().splitlines()) == 20



def test_cancel_terminates_process_group(tmp_path: Path):
    output = tmp_path / "cancel-ticks.txt"
    script = tmp_path / "cancel_writer.py"
    script.write_text(
        "import pathlib, time\n"
        f"p = pathlib.Path({str(output)!r})\n"
        "for i in range(100):\n"
        "    with p.open('a') as f:\n"
        "        f.write(str(i) + '\\n')\n"
        "        f.flush()\n"
        "    time.sleep(0.05)\n"
    )
    cancel = threading.Event()
    errors = []

    def runner():
        try:
            _run([sys.executable, "-u", str(script)], cancel.is_set)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    deadline = time.time() + 2
    while time.time() < deadline:
        if output.exists() and len(output.read_text().splitlines()) >= 3:
            break
        time.sleep(0.02)
    cancel.set()
    thread.join(3)
    assert not thread.is_alive()
    assert errors
    assert str(errors[0]) == "cancelled"
    assert len(output.read_text().splitlines()) < 100


def test_engine_error_includes_sanitized_tail():
    import pytest
    from transfer_hub.engines import EngineError

    with pytest.raises(EngineError) as exc:
        _run(
            [sys.executable, "-u", "-c", "import sys; print('403 Forbidden https://example.com/a?token=secret'); sys.exit(1)"],
            lambda: False,
        )
    message = str(exc.value)
    assert "403 Forbidden" in message
    assert "token=secret" not in message
    assert "<redacted>" in message


def test_pack_zip_aes_uses_aes256(tmp_path: Path):
    import shutil
    import subprocess
    import pytest
    from transfer_hub.engines import pack_zip_aes

    exe = shutil.which("7z") or shutil.which("7zz")
    if not exe:
        pytest.skip("7z is not installed")
    source = tmp_path / "payload.txt"
    source.write_text("hello secure zip")
    archive = tmp_path / "payload.zip"
    password = "StrongPassword1234567890"
    parts = pack_zip_aes(source, archive, password, None, lambda: False)
    assert parts == [archive]
    listing = subprocess.check_output([exe, "l", "-slt", f"-p{password}", str(archive)], text=True)
    assert "Encrypted = +" in listing
    assert "AES-256" in listing


def test_nm3u8dl_uses_concat_demuxer(tmp_path: Path, monkeypatch):
    from transfer_hub import engines

    monkeypatch.setattr(engines.shutil, "which", lambda name: "/usr/local/bin/N_m3u8DL-RE" if name == "N_m3u8DL-RE" else None)
    captured = {}

    def fake_run(argv, cancel, progress=None, process_started=None, process_ended=None):
        captured["argv"] = argv
        save_dir = Path(argv[argv.index("--save-dir") + 1])
        save_name = argv[argv.index("--save-name") + 1]
        (save_dir / f"{save_name}.mp4").write_bytes(b"ok")

    monkeypatch.setattr(engines, "_run", fake_run)
    result = engines.download(
        "nm3u8dl",
        "https://example.com/video.m3u8",
        tmp_path,
        "video.m3u8",
        {},
        lambda: False,
    )
    assert result == tmp_path / "video.mp4"
    assert "--use-ffmpeg-concat-demuxer" in captured["argv"]


def test_archive_passwords_are_not_placed_in_argv(tmp_path: Path, monkeypatch):
    from transfer_hub import engines

    source = tmp_path / "payload.txt"
    source.write_text("payload")
    archive = tmp_path / "payload.zip"
    password = "never-in-process-argv"
    seen = []

    monkeypatch.setattr(engines.shutil, "which", lambda name: "/usr/bin/7z" if name in {"7z", "7zz"} else None)

    def fake_prompt(argv, supplied, cancel):
        seen.append((list(argv), supplied))
        if argv[1] == "a":
            archive.write_bytes(b"archive")
        return []

    monkeypatch.setattr(engines, "_run_password_prompt", fake_prompt)
    engines.pack_zip_aes(source, archive, password, None, lambda: False)
    engines.verify_7z_password(archive, password, lambda: False)

    assert len(seen) == 2
    for argv, supplied in seen:
        assert supplied == password
        assert password not in " ".join(argv)
    assert seen[0][0][seen[0][0].index("-p")] == "-p"
    assert seen[1][0][1] == "t"
