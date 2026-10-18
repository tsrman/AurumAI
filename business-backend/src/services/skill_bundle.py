"""Validate and parse uploaded "code" skill bundles.

A bundle is a .zip modeled on Anthropic's Agent Skills format
(https://github.com/anthropics/skills): a top-level SKILL.md with YAML
frontmatter (name/description) plus a markdown instructions body, and
optional scripts/reference/resources files. This module only validates the
archive and extracts SKILL.md's metadata — it never extracts files to disk
and never executes anything. Actual extraction happens later, inside the
isolated skill-executor sandbox (see project plan), which re-verifies the
checksum computed here before trusting the archive.
"""

import hashlib
import io
import os
import zipfile
from dataclasses import dataclass

import frontmatter

from src.core.config import settings

SKILL_MD_NAMES = {"SKILL.md", "./SKILL.md"}


class SkillBundleError(Exception):
    """Any bundle validation failure — callers should surface this as a 400."""


@dataclass
class ParsedBundle:
    raw_bytes: bytes
    checksum: str
    size_bytes: int
    skill_name: str
    skill_description: str
    skill_md_body: str


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _is_safe_path(name: str) -> bool:
    if os.path.isabs(name):
        return False
    normalized = os.path.normpath(name)
    if normalized == ".." or normalized.startswith(f"..{os.sep}") or normalized.startswith("../"):
        return False
    return True


def validate_and_parse(raw_bytes: bytes) -> ParsedBundle:
    if len(raw_bytes) > settings.SKILL_BUNDLE_MAX_BYTES:
        raise SkillBundleError(
            f"Bundle exceeds max size of {settings.SKILL_BUNDLE_MAX_BYTES} bytes"
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillBundleError("Uploaded file is not a valid zip archive") from exc

    infos = zf.infolist()
    if len(infos) > settings.SKILL_BUNDLE_MAX_ENTRIES:
        raise SkillBundleError(
            f"Bundle contains too many entries (max {settings.SKILL_BUNDLE_MAX_ENTRIES})"
        )

    total_uncompressed = 0
    total_compressed = 0
    for info in infos:
        if not _is_safe_path(info.filename):
            raise SkillBundleError(f"Unsafe path in bundle: {info.filename}")
        if _is_symlink(info):
            raise SkillBundleError(f"Symlinks are not allowed in bundles: {info.filename}")
        total_uncompressed += info.file_size
        total_compressed += info.compress_size

    if total_uncompressed > settings.SKILL_BUNDLE_MAX_UNCOMPRESSED_BYTES:
        raise SkillBundleError("Bundle uncompressed size exceeds limit")
    if (
        total_compressed > 0
        and total_uncompressed / total_compressed > settings.SKILL_BUNDLE_MAX_COMPRESSION_RATIO
    ):
        raise SkillBundleError("Bundle compression ratio is too high (possible zip bomb)")

    skill_md_name = next((n for n in zf.namelist() if n in SKILL_MD_NAMES), None)
    if skill_md_name is None:
        raise SkillBundleError("Bundle must contain a top-level SKILL.md file")

    skill_md_raw = zf.read(skill_md_name).decode("utf-8", errors="replace")
    try:
        post = frontmatter.loads(skill_md_raw)
    except Exception as exc:
        raise SkillBundleError(f"Could not parse SKILL.md frontmatter: {exc}") from exc

    name = post.metadata.get("name")
    description = post.metadata.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillBundleError("SKILL.md frontmatter must include a non-empty 'name'")
    if not isinstance(description, str) or not description.strip():
        raise SkillBundleError("SKILL.md frontmatter must include a non-empty 'description'")
    if len(description) > 2000:
        raise SkillBundleError("SKILL.md description is too long (max 2000 chars)")

    return ParsedBundle(
        raw_bytes=raw_bytes,
        checksum=hashlib.sha256(raw_bytes).hexdigest(),
        size_bytes=len(raw_bytes),
        skill_name=name.strip(),
        skill_description=description.strip(),
        skill_md_body=post.content,
    )
