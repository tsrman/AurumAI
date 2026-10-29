"""Platform code skills hydrated into a Hermes sandbox before each turn
(docs/HERMES_AGENT.md, phase 5c).

business-backend lists the code skills the conversation owner may read; each
bundle is fetched here, checked against its checksum and unpacked into a
staging directory of the sandbox, which `hermes-relay skills-hydrate` swaps
into Hermes's external skills directory, read-only. Bundles were validated
when they were stored; they are bounded again here, well below the workspace
limit, and a skill over the bounds or whose bundle does not verify is left
out rather than failing the turn.
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

SANDBOX_UID = 10001
MAX_SKILLS = 32
MAX_SKILL_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 512
MAX_COMPRESSION_RATIO = 10
_DIR = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class PlatformSkill:
    dir: str
    bundle_storage_key: str
    bundle_checksum: str


class Unusable(Exception):
    pass


def _files(bundle: bytes) -> list[tuple[str, bytes, bool]]:
    """(path, data, executable) for every file, or Unusable."""
    try:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES:
                raise Unusable()
            size = sum(info.file_size for info in infos)
            compressed = sum(info.compress_size for info in infos)
            if size > MAX_SKILL_BYTES or (compressed and size / compressed > MAX_COMPRESSION_RATIO):
                raise Unusable()
            files = []
            for info in infos:
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                name = info.filename.removeprefix("./")
                parts = name.split("/")
                if (mode & 0o170000) == 0o120000 or name.startswith("/") or any(part in ("", ".", "..") for part in parts):
                    raise Unusable()
                files.append((name, archive.read(info), bool(mode & 0o111)))
    except (zipfile.BadZipFile, ValueError):
        raise Unusable() from None
    if "SKILL.md" not in {name for name, _, _ in files}:
        raise Unusable()
    return files


async def staging_tar(
    skills: list[PlatformSkill], fetch: Callable[[str], Awaitable[bytes]], staging: str
) -> tuple[bytes, int, int]:
    """The tar to put at /workspace, with the hydrated and skipped counts."""
    buffer = io.BytesIO()
    hydrated = skipped = total = 0
    with tarfile.open(fileobj=buffer, mode="w") as out:

        def directory(path: str) -> None:
            info = tarfile.TarInfo(path)
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, SANDBOX_UID, SANDBOX_UID
            out.addfile(info)

        # Parents the archive leaves out would be created as root.
        directory(staging.rsplit("/", 1)[0])
        directory(staging)
        seen: set[str] = set()
        for skill in skills[:MAX_SKILLS]:
            if not _DIR.match(skill.dir) or skill.dir in seen:
                skipped += 1
                continue
            try:
                bundle = await fetch(skill.bundle_storage_key)
                if hashlib.sha256(bundle).hexdigest() != skill.bundle_checksum:
                    raise Unusable()
                files = _files(bundle)
            except Exception:
                # A bundle that cannot be fetched or verified is not offered.
                skipped += 1
                continue
            size = sum(len(data) for _, data, _ in files)
            if total + size > MAX_TOTAL_BYTES:
                skipped += 1
                continue
            total += size
            seen.add(skill.dir)
            hydrated += 1
            base = f"{staging}/{skill.dir}"
            directories = {base} | {
                f"{base}/{'/'.join(name.split('/')[:depth])}"
                for name, _, _ in files
                for depth in range(1, name.count("/") + 1)
            }
            for path in sorted(directories):
                directory(path)
            for name, data, executable in files:
                info = tarfile.TarInfo(f"{base}/{name}")
                info.size, info.mode, info.uid, info.gid = len(data), 0o755 if executable else 0o644, SANDBOX_UID, SANDBOX_UID
                out.addfile(info, io.BytesIO(data))
        skipped += max(0, len(skills) - MAX_SKILLS)
    return buffer.getvalue(), hydrated, skipped
