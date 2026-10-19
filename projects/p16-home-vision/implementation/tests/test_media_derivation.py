from pathlib import Path

from media_derivation.pipeline import (
    DerivationPaths,
    DerivationPlan,
    build_ffmpeg_command,
    parse_showinfo_manifest,
)


def test_build_command_stream_copies_web_video_and_extracts_frames_and_audio(tmp_path: Path):
    paths = DerivationPaths(raw=tmp_path / "raw.mpeg", root=tmp_path / "asset")
    cmd = build_ffmpeg_command(paths, DerivationPlan(has_audio=True))

    assert ["-c:v", "copy"] == cmd[cmd.index("-c:v") : cmd.index("-c:v") + 2]
    assert "hvc1" in cmd
    assert "fps=fps=1.0,showinfo" in cmd
    assert str(paths.audio_part) in cmd
    assert str(paths.web_part) in cmd


def test_build_command_skips_audio_output_when_source_has_no_audio(tmp_path: Path):
    paths = DerivationPaths(raw=tmp_path / "raw.mpeg", root=tmp_path / "asset")
    cmd = build_ffmpeg_command(paths, DerivationPlan(has_audio=False))

    assert str(paths.audio_part) not in cmd
    assert "pcm_s16le" not in cmd


def test_parse_showinfo_uses_pts_not_frame_index():
    stderr = "\n".join(
        [
            "[Parsed_showinfo_1] n:   0 pts:100 pts_time:0.125 pos:0",
            "[Parsed_showinfo_1] n:   1 pts:900 pts_time:1.875 pos:1",
        ]
    )

    rows = parse_showinfo_manifest(
        stderr,
        ["00000001.jpg", "00000002.jpg"],
        source_start_epoch=1_700_000_000.0,
    )

    assert rows[0]["source_pts_seconds"] == 0.125
    assert rows[1]["source_pts_seconds"] == 1.875
    assert rows[1]["absolute_epoch_seconds"] == 1_700_000_001.875
