from pathlib import Path

from app.domain.thumbnails import generate_thumbnail, thumbnail_is_usable, thumbnail_path


def test_thumbnail_path_is_same_volume_and_generation_is_idempotent(tmp_path, monkeypatch) -> None:
    source = tmp_path / "recordings" / "2026" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video-fixture")
    output = thumbnail_path(tmp_path, "asset-fixture")
    calls = []

    class Completed:
        returncode = 0
        stderr = "lavfi.signalstats.YLOW=10\nlavfi.signalstats.YHIGH=200\nlavfi.signalstats.SATAVG=20\n"

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] != "-":
            Path(command[-1]).write_bytes(b"jpeg-fixture")
        return Completed()

    monkeypatch.setattr("app.domain.thumbnails.subprocess.run", fake_run)
    assert output == tmp_path / "recordings" / "thumbnails" / "asset-fixture.jpg"
    assert generate_thumbnail(source, output) == "created"
    assert generate_thumbnail(source, output) == "already_present"
    assert output.read_bytes() == b"jpeg-fixture"
    assert len(calls) == 1
    assert not output.with_suffix(".jpg.part").exists()


def test_thumbnail_generation_falls_back_to_first_decodable_frame(tmp_path, monkeypatch) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video-fixture")
    output = thumbnail_path(tmp_path, "asset-fallback")
    seeks = []

    class Completed:
        def __init__(self, returncode, stderr=""):
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(command, **kwargs):
        if command[-1] == "-":
            return Completed(0, "lavfi.signalstats.YLOW=10\nlavfi.signalstats.YHIGH=200\nlavfi.signalstats.SATAVG=20\n")
        seek = command[command.index("-ss") + 1]
        seeks.append(seek)
        if seek == "0":
            Path(command[-1]).write_bytes(b"jpeg-fixture")
            return Completed(0)
        return Completed(1)

    monkeypatch.setattr("app.domain.thumbnails.subprocess.run", fake_run)
    assert generate_thumbnail(source, output) == "created"
    assert seeks == ["10", "5", "2", "1", "0.5", "0"]
    assert output.read_bytes() == b"jpeg-fixture"


def test_thumbnail_generation_rejects_uniform_frame_before_replace(tmp_path, monkeypatch) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video-fixture")
    output = thumbnail_path(tmp_path, "asset-repair")
    output.parent.mkdir(parents=True)
    output.write_bytes(b"old-jpeg")
    seeks = []

    class Completed:
        returncode = 0

        def __init__(self, stderr):
            self.stderr = stderr

    def fake_run(command, **kwargs):
        seek = command[command.index("-ss") + 1]
        seeks.append(seek)
        Path(command[-1]).write_bytes(b"candidate-jpeg")
        stderr = "lavfi.signalstats.YLOW=130\nlavfi.signalstats.YHIGH=130\nlavfi.signalstats.SATAVG=0.1\n"
        if seek == "1":
            stderr = "lavfi.signalstats.YLOW=10\nlavfi.signalstats.YHIGH=200\nlavfi.signalstats.SATAVG=20\n"
        return Completed(stderr)

    monkeypatch.setattr("app.domain.thumbnails.subprocess.run", fake_run)
    assert generate_thumbnail(source, output, overwrite=True, reject_uniform=True) == "created"
    assert seeks == ["10", "5", "2", "1"]
    assert output.read_bytes() == b"candidate-jpeg"
    assert not output.with_suffix(".jpg.part").exists()


def test_thumbnail_is_usable_rejects_decode_failure_and_uniform_frame(tmp_path, monkeypatch) -> None:
    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"jpeg-fixture")

    class Completed:
        returncode = 0
        stdout = "lavfi.signalstats.YLOW=130\nlavfi.signalstats.YHIGH=130\nlavfi.signalstats.SATAVG=0.1\n"
        stderr = ""

    monkeypatch.setattr("app.domain.thumbnails.subprocess.run", lambda *_args, **_kwargs: Completed())
    assert not thumbnail_is_usable(image)

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "decode failed"

    monkeypatch.setattr("app.domain.thumbnails.subprocess.run", lambda *_args, **_kwargs: Failed())
    assert not thumbnail_is_usable(image)
