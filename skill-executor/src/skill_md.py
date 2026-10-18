"""Extract SKILL.md's instructions body from an already-validated bundle.

business-backend's skill_bundle.py already enforces that SKILL.md exists at
the archive root and has valid frontmatter before the bundle is ever stored
— this just re-reads it here so the sandbox session can hand the LLM the
actual instructions text.
"""

import io
import zipfile

import frontmatter

SKILL_MD_NAMES = {"SKILL.md", "./SKILL.md"}


class SkillMdError(Exception):
    pass


def extract_skill_md_body(bundle_bytes: bytes) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    except zipfile.BadZipFile as exc:
        raise SkillMdError(f"Bundle is not a valid zip archive: {exc}") from exc

    name = next((n for n in zf.namelist() if n in SKILL_MD_NAMES), None)
    if name is None:
        raise SkillMdError("Bundle has no top-level SKILL.md")

    raw = zf.read(name).decode("utf-8", errors="replace")
    try:
        post = frontmatter.loads(raw)
    except Exception as exc:
        raise SkillMdError(f"Could not parse SKILL.md frontmatter: {exc}") from exc
    return post.content
