"""Hermes workspace snapshots (docs/HERMES_AGENT.md, phase 2c-ii).

A snapshot is a plain tar of the sandbox's /workspace in the business bucket,
stored by its own SHA-256 under ``hermes-sessions/{org}/{conversation}/``, next
to a ``latest.json`` pointer. Its content is the agent's and therefore
untrusted, both when it is taken and when it is restored: every archive goes
through `sanitize`, which keeps only regular files and directories under
relative paths, gives them the sandbox user and fixed modes, and bounds the
entry count and the total size. Platform secrets (the generated config, the
proxy URL with the session token, relay state) and the per-user memory and
skills never enter a snapshot.

Until phase 4 records the checksum in the session row, the pointer object is
the reference a restore is checked against.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aioboto3
from botocore.config import Config

from src.config import Settings

PREFIX = "hermes-sessions"
SANDBOX_UID = 10001
# Relative to /workspace. Secrets the platform writes, relay state that only
# makes sense in the sandbox that wrote it, and what follows the per-user flow.
EXCLUDED = (
    ".aurum",
    ".hermes/config.yaml",
    ".hermes/.env",
    ".hermes/auth.json",
    ".hermes/logs",
    ".hermes/memories",
    ".hermes/skills",
)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_KEY_PART = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_POINTER_MAX_BYTES = 4096
_CHUNK = 64 * 1024


class SnapshotError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    sha256: str
    size: int
    created_at: float


def _excluded(name: str) -> bool:
    return any(name == path or name.startswith(path + "/") for path in EXCLUDED)


def _relative(name: str, strip: str | None) -> str | None:
    """The entry's path below the workspace, or None when it is not a safe one."""
    if "\x00" in name or "\\" in name or name.startswith("/"):
        return None
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if strip is not None:
        if not parts or parts[0] != strip:
            return None
        parts = parts[1:]
    if not parts or ".." in parts:
        return None
    path = "/".join(parts)
    return path if len(path) <= 4096 else None


def sanitize(source: Path, target: Path, *, strip: str | None, max_bytes: int, max_entries: int) -> Snapshot:
    """Rewrite the tar at `source` as a clean workspace archive at `target`.

    `strip` is the top-level directory Docker's archive API puts entries under.
    Links, devices and anything outside the workspace are dropped; a missing
    parent directory is added, so Docker never creates one owned by root.
    """
    digest = hashlib.sha256()
    total = entries = 0
    directories: set[str] = set()

    def add(out: tarfile.TarFile, info: tarfile.TarInfo, data: Any = None) -> None:
        nonlocal entries
        entries += 1
        if entries > max_entries:
            raise SnapshotError("hermes_snapshot_too_large")
        out.addfile(info, data)

    def directory(out: tarfile.TarFile, path: str, mtime: int) -> None:
        for depth in range(1, path.count("/") + 2):
            parent = "/".join(path.split("/")[:depth])
            if parent in directories:
                continue
            info = tarfile.TarInfo(parent)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o700, mtime
            info.uid = info.gid = SANDBOX_UID
            add(out, info)
            directories.add(parent)

    try:
        with tarfile.open(source, mode="r|") as archive, target.open("xb") as raw:
            target.chmod(0o600)
            with tarfile.open(fileobj=raw, mode="w|", format=tarfile.PAX_FORMAT) as out:
                for member in archive:
                    path = _relative(member.name, strip)
                    if path is None or _excluded(path):
                        continue
                    mtime = max(0, min(int(member.mtime), 2**33 - 1))
                    if member.isdir():
                        directory(out, path, mtime)
                    elif member.isreg():
                        total += member.size
                        if total > max_bytes:
                            raise SnapshotError("hermes_snapshot_too_large")
                        if "/" in path:
                            directory(out, path.rsplit("/", 1)[0], mtime)
                        info = tarfile.TarInfo(path)
                        info.size, info.mtime = member.size, mtime
                        info.mode = 0o700 if member.mode & 0o100 else 0o600
                        info.uid = info.gid = SANDBOX_UID
                        add(out, info, archive.extractfile(member))
    except SnapshotError:
        target.unlink(missing_ok=True)
        raise
    except (tarfile.TarError, OSError, ValueError):
        target.unlink(missing_ok=True)
        raise SnapshotError("hermes_snapshot_invalid") from None
    with target.open("rb") as written:
        while block := written.read(_CHUNK):
            digest.update(block)
    return Snapshot(sha256=digest.hexdigest(), size=target.stat().st_size, created_at=time.time())


def prefix(org_id: str, conversation_id: str) -> str:
    if not (_KEY_PART.fullmatch(org_id) and _KEY_PART.fullmatch(conversation_id)):
        raise SnapshotError("hermes_snapshot_key_invalid")
    return f"{PREFIX}/{org_id}/{conversation_id}/"


class SnapshotStore:
    """Snapshot objects, written with skill-executor's credentials, which
    operators scope to the ``hermes-sessions/`` prefix for writes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = aioboto3.Session()

    @property
    def enabled(self) -> bool:
        return self._settings.hermes_snapshot_enabled and bool(self._settings.s3_endpoint)

    def _client(self) -> Any:
        return self._session.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            region_name=self._settings.s3_region,
            # A store that is down must fail a snapshot quickly: the reaper waits on it.
            config=Config(connect_timeout=5, read_timeout=60, retries={"total_max_attempts": 2}),
        )

    async def put(self, org_id: str, conversation_id: str, path: Path, snapshot: Snapshot, session_id: str) -> None:
        """Upload the archive, then point `latest.json` at it, then prune."""
        base = prefix(org_id, conversation_id)
        pointer = {
            "sha256": snapshot.sha256,
            "size": snapshot.size,
            "created_at": snapshot.created_at,
            "session_id": session_id,
        }
        bucket = self._settings.s3_bucket
        try:
            async with self._client() as s3:
                with path.open("rb") as body:
                    await s3.put_object(
                        Bucket=bucket, Key=f"{base}{snapshot.sha256}.tar", Body=body, ContentLength=snapshot.size
                    )
                await s3.put_object(Bucket=bucket, Key=f"{base}latest.json", Body=json.dumps(pointer).encode())
                await self._prune(s3, base, snapshot.sha256)
        except SnapshotError:
            raise
        except Exception:
            raise SnapshotError("hermes_snapshot_store_failed") from None

    async def _prune(self, s3: Any, base: str, current: str) -> None:
        listed = await s3.list_objects_v2(Bucket=self._settings.s3_bucket, Prefix=base)
        archives = sorted(
            (item for item in listed.get("Contents") or [] if item["Key"].endswith(".tar")),
            key=lambda item: item["LastModified"],
            reverse=True,
        )
        keep = {f"{base}{current}.tar"}
        keep.update(item["Key"] for item in archives[: max(1, self._settings.hermes_snapshot_keep)])
        for item in archives:
            if item["Key"] not in keep:
                await s3.delete_object(Bucket=self._settings.s3_bucket, Key=item["Key"])

    async def latest(self, org_id: str, conversation_id: str) -> str | None:
        """The SHA-256 `latest.json` names, or None when there is no snapshot."""
        key = f"{prefix(org_id, conversation_id)}latest.json"
        try:
            async with self._client() as s3:
                try:
                    response = await s3.get_object(Bucket=self._settings.s3_bucket, Key=key)
                except s3.exceptions.NoSuchKey:
                    return None
                body = response["Body"]
                async with body:
                    data = await body.read(_POINTER_MAX_BYTES + 1)
        except Exception:
            raise SnapshotError("hermes_snapshot_store_failed") from None
        try:
            sha = json.loads(data)["sha256"] if len(data) <= _POINTER_MAX_BYTES else None
        except (ValueError, TypeError, KeyError):
            sha = None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise SnapshotError("hermes_snapshot_invalid")
        return sha

    async def fetch(self, org_id: str, conversation_id: str, sha256: str, path: Path, limit: int) -> None:
        """Download one archive to `path` and check it against its name."""
        if not _SHA.fullmatch(sha256):
            raise SnapshotError("hermes_snapshot_invalid")
        key = f"{prefix(org_id, conversation_id)}{sha256}.tar"
        digest = hashlib.sha256()
        size = 0
        try:
            async with self._client() as s3:
                try:
                    response = await s3.get_object(Bucket=self._settings.s3_bucket, Key=key)
                except s3.exceptions.NoSuchKey:
                    raise SnapshotError("hermes_snapshot_not_found") from None
                if (response.get("ContentLength") or 0) > limit:
                    raise SnapshotError("hermes_snapshot_too_large")
                with path.open("xb") as target:
                    path.chmod(0o600)
                    body = response["Body"]
                    async with body:
                        while block := await body.read(_CHUNK):
                            size += len(block)
                            if size > limit:
                                raise SnapshotError("hermes_snapshot_too_large")
                            digest.update(block)
                            target.write(block)
        except SnapshotError:
            path.unlink(missing_ok=True)
            raise
        except Exception:
            path.unlink(missing_ok=True)
            raise SnapshotError("hermes_snapshot_store_failed") from None
        if digest.hexdigest() != sha256:
            path.unlink(missing_ok=True)
            raise SnapshotError("hermes_snapshot_invalid")

    async def sweep(self, cutoff: float) -> int:
        """Delete snapshot objects last written before `cutoff` (the retention)."""
        removed = 0
        bucket = self._settings.s3_bucket
        before = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=bucket, Prefix=f"{PREFIX}/"):
                for item in page.get("Contents") or []:
                    if item["LastModified"] < before:
                        await s3.delete_object(Bucket=bucket, Key=item["Key"])
                        removed += 1
        return removed


async def to_thread_sanitize(source: Path, target: Path, *, strip: str | None, settings: Settings) -> Snapshot:
    return await asyncio.to_thread(
        sanitize,
        source,
        target,
        strip=strip,
        max_bytes=settings.hermes_snapshot_max_bytes,
        max_entries=settings.hermes_snapshot_max_entries,
    )
