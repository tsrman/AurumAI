from __future__ import annotations

import fnmatch
import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class ReleaseManifest:
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    new_files: tuple[str, ...]
    required: tuple[str, ...]


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class ExportInventory:
    records: tuple[FileRecord, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(record.path for record in self.records)


def _safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and not any(part in {"..", ".", ""} for part in value.split("/"))
        and "\\" not in value
        and "\x00" not in value
    )


def prohibited_path(value: str) -> bool:
    parts = PurePosixPath(value).parts
    forbidden = {
        ".git", ".worktrees", ".superpowers", ".superdesign", ".codex", ".agents",
        ".venv", "venv", "node_modules", ".next", "__pycache__", "playwright-report",
        "test-results", "blob-report", ".aws", ".ssh", ".pytest_cache", ".mypy_cache",
        ".ruff_cache",
        ".aurumai-runtime", ".oss-release",
    }
    return (
        any(part.casefold() in forbidden for part in parts)
        or any(part.casefold().startswith(".env") and part != ".env.example" for part in parts)
        or PurePosixPath(value).name.casefold() in {"dev_log.md", "review.md", "kimi_review.md"}
        or value.casefold().endswith((".log", ".har", ".pem", ".p12", ".sqlite", ".db"))
        or value.startswith("docs/superpowers/")
    )


def load_manifest(path: Path) -> ReleaseManifest:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported_manifest_version")
    if set(data) != {"schema_version", "include", "exclude", "new_files", "required"}:
        raise ValueError("invalid_manifest_fields")
    for key in ("include", "exclude", "new_files", "required"):
        values = data[key]
        if not isinstance(values, list) or any(not isinstance(v, str) or not _safe_path(v) for v in values):
            raise ValueError("unsafe_manifest_path")
        if key in {"new_files", "required"} and any(any(c in v for c in "*?[") for v in values):
            raise ValueError("unsafe_manifest_path")
        if len(values) != len(set(values)):
            raise ValueError("duplicate_manifest_path")
    return ReleaseManifest(*(tuple(data[key]) for key in ("include", "exclude", "new_files", "required")))


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def select_files(source: Path, manifest: ReleaseManifest) -> tuple[str, ...]:
    tracked = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--cached", "-z"],
        check=True, capture_output=True,
    ).stdout.decode().split("\0")
    candidates = set(tracked) | set(manifest.new_files)
    selected = tuple(sorted(
        name for name in candidates
        if name and _safe_path(name) and not prohibited_path(name)
        and _matches(name, manifest.include) and not _matches(name, manifest.exclude)
        and ((source / name).exists() or (source / name).is_symlink())
    ))
    if not set(manifest.required).issubset(selected):
        raise ValueError("missing_required_file")
    return selected


def _read_regular(source: Path, name: str) -> tuple[bytes, int]:
    path = source / name
    for parent in (path, *path.parents):
        if parent == source:
            break
        if parent.is_symlink():
            raise ValueError("unsafe_source_file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ValueError("unsafe_source_file") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
            raise ValueError("unsafe_source_file")
        data = stream.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("unsafe_source_file")
    return data, 0o755 if info.st_mode & 0o111 else 0o644


def _publish_directory(temporary: Path, destination: Path) -> None:
    # Linux renameat2 provides an atomic no-replace operation; fail closed
    # on unsupported hosts/filesystems instead of falling back to rename.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "atomic_publish_unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "atomic_publish_failed")


def export_candidate(source: Path, destination: Path, manifest: ReleaseManifest) -> ExportInventory:
    source = source.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("destination_exists")
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("overlapping_export_roots")
    paths = select_files(source, manifest)
    contents = [(name, *_read_regular(source, name)) for name in paths]
    records = tuple(FileRecord(name, hashlib.sha256(data).hexdigest(), len(data), mode) for name, data, mode in contents)
    temporary = Path(tempfile.mkdtemp(prefix=".aurumai-export-", dir=destination.parent))
    try:
        for name, data, mode in contents:
            output = temporary / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            output.chmod(mode)
        from .audit import audit_tree
        if audit_tree(temporary, manifest):
            raise ValueError("release_audit_failed")
        # A competing operator must never have their candidate replaced.
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("destination_exists")
        _publish_directory(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return ExportInventory(records)
