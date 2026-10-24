from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath


MAX_ARCHIVE_FILES = 1000
MAX_ARCHIVE_BYTES = 10 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 2 * 1024 * 1024 * 1024


class ArchiveError(ValueError):
    pass


def is_supported_archive(path: Path) -> bool:
    if zipfile.is_zipfile(path):
        return True
    try:
        with path.open("rb") as source:
            return source.read(6) == b"7z\xbc\xaf'\x1c"
    except OSError:
        return False


def _safe_member(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if "\0" in normalized or path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveError("archive contains an unsafe path")
    if re.match(r"^[A-Za-z]:", path.parts[0]):
        raise ArchiveError("archive contains an unsafe path")
    return path


def _target(root: Path, member: PurePosixPath) -> Path:
    target = root.joinpath(*member.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ArchiveError("archive path escapes extraction directory")
    return target


def _copy_limited(source, target: Path, count: int, total: int) -> tuple[int, int]:
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as out:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_ARCHIVE_FILE_BYTES or total + written > MAX_ARCHIVE_BYTES:
                raise ArchiveError("archive exceeds extraction size limit")
            out.write(chunk)
    return count + 1, total + written


def extract_archive(archive: Path, destination: Path) -> list[Path]:
    """Extract a supported archive into an empty job-owned directory with strict limits."""
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or any(destination.iterdir()):
        raise ArchiveError("extraction directory must be empty")
    files: list[Path] = []
    total = 0
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as zf:
                entries = zf.infolist()
                regular = []
                for info in entries:
                    member = _safe_member(info.filename)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if mode and (mode & 0o170000) not in (0, 0o100000, 0o040000):
                        raise ArchiveError("archive contains a link or special file")
                    if info.is_dir():
                        _target(destination, member).mkdir(parents=True, exist_ok=True)
                        continue
                    if info.file_size > MAX_ARCHIVE_FILE_BYTES:
                        raise ArchiveError("archive member exceeds extraction size limit")
                    regular.append((info, member))
                if len(regular) > MAX_ARCHIVE_FILES:
                    raise ArchiveError("archive contains too many files")
                for info, member in regular:
                    path = _target(destination, member)
                    with zf.open(info) as source:
                        _, total = _copy_limited(source, path, len(files), total)
                    files.append(path)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            if isinstance(exc, ArchiveError):
                raise
            raise ArchiveError(f"could not extract ZIP archive: {exc}") from exc
    else:
        executable = shutil.which("7z") or shutil.which("7zz")
        if not executable:
            raise ArchiveError("7z extraction is unavailable on this server")
        listing = subprocess.run([executable, "l", "-slt", str(archive)], capture_output=True, text=True)
        if listing.returncode:
            raise ArchiveError("could not inspect 7z archive")
        records: list[dict[str, str]] = []
        for block in listing.stdout.split("\n\n"):
            record = {}
            for line in block.splitlines():
                if " = " in line:
                    key, value = line.split(" = ", 1)
                    record[key] = value
            if "Path" in record and "Size" in record:
                records.append(record)
        regular = []
        for record in records:
            member = _safe_member(record["Path"])
            if record.get("Symbolic Link") or record.get("Hard Link"):
                raise ArchiveError("archive contains a link")
            if record.get("Folder") == "+":
                _target(destination, member).mkdir(parents=True, exist_ok=True)
                continue
            try:
                size = int(record.get("Size", "0"))
            except ValueError as exc:
                raise ArchiveError("archive has invalid member size") from exc
            if size > MAX_ARCHIVE_FILE_BYTES:
                raise ArchiveError("archive member exceeds extraction size limit")
            regular.append((record["Path"], member, size))
        if len(regular) > MAX_ARCHIVE_FILES or sum(x[2] for x in regular) > MAX_ARCHIVE_BYTES:
            raise ArchiveError("archive exceeds extraction limits")
        for original, member, expected_size in regular:
            if original.startswith("-"):
                raise ArchiveError("archive contains an unsafe path")
            proc = subprocess.Popen([executable, "x", "-so", str(archive), original], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            assert proc.stdout
            path = _target(destination, member)
            try:
                _, total = _copy_limited(proc.stdout, path, len(files), total)
                if proc.wait() != 0:
                    path.unlink(missing_ok=True)
                    raise ArchiveError("could not extract 7z member")
                if path.stat().st_size != expected_size:
                    path.unlink(missing_ok=True)
                    raise ArchiveError("7z member size did not match archive metadata")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            files.append(path)
    if not files:
        raise ArchiveError("archive contains no regular files")
    return files
