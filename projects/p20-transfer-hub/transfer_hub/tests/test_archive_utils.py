from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from transfer_hub import archive_utils
from transfer_hub.archive_utils import ArchiveError, extract_archive


def make_zip(path: Path, entries: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)


def test_extract_zip_to_job_directory(tmp_path: Path):
    archive = tmp_path / "input.zip"
    make_zip(archive, [("folder/file.txt", b"phone-ready")])

    files = extract_archive(archive, tmp_path / "out")

    assert [p.relative_to(tmp_path / "out").as_posix() for p in files] == ["folder/file.txt"]
    assert files[0].read_bytes() == b"phone-ready"


@pytest.mark.parametrize("member", ["../escape.txt", "folder/../../escape.txt", "C:/escape.txt", "C:escape.txt", r"..\\escape.txt", "/absolute.txt"])
def test_rejects_traversal_and_absolute_paths(tmp_path: Path, member: str):
    archive = tmp_path / "input.zip"
    make_zip(archive, [(member, b"no")])
    destination = tmp_path / "out"

    with pytest.raises(ArchiveError, match="unsafe path"):
        extract_archive(archive, destination)

    assert not (tmp_path / "escape.txt").exists()


def test_rejects_zip_symlink(tmp_path: Path):
    archive = tmp_path / "input.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, "../../outside")

    with pytest.raises(ArchiveError, match="link or special"):
        extract_archive(archive, tmp_path / "out")


def test_rejects_file_count_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    archive = tmp_path / "input.zip"
    make_zip(archive, [("one", b"1"), ("two", b"2")])
    monkeypatch.setattr(archive_utils, "MAX_ARCHIVE_FILES", 1)

    with pytest.raises(ArchiveError, match="too many files"):
        extract_archive(archive, tmp_path / "out")


def test_rejects_member_and_total_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    archive = tmp_path / "input.zip"
    make_zip(archive, [("large", b"12345")])
    destination = tmp_path / "member-out"
    monkeypatch.setattr(archive_utils, "MAX_ARCHIVE_FILE_BYTES", 4)

    with pytest.raises(ArchiveError, match="member exceeds"):
        extract_archive(archive, destination)

    monkeypatch.setattr(archive_utils, "MAX_ARCHIVE_FILE_BYTES", 10)
    monkeypatch.setattr(archive_utils, "MAX_ARCHIVE_BYTES", 4)
    with pytest.raises(ArchiveError, match="extraction size limit"):
        extract_archive(archive, tmp_path / "total-out")


def test_requires_empty_destination(tmp_path: Path):
    archive = tmp_path / "input.zip"
    make_zip(archive, [("file", b"ok")])
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "existing").write_text("keep")

    with pytest.raises(ArchiveError, match="must be empty"):
        extract_archive(archive, destination)
