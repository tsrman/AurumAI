import io
import zipfile

import pytest

from src.services.skill_bundle import SkillBundleError, validate_and_parse


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


SKILL_MD = (
    b"---\n"
    b"name: greet\n"
    b"description: Greets the user by name.\n"
    b"---\n"
    b"# Greet\n"
    b"Run scripts/greet.py.\n"
)


def test_valid_bundle_round_trips():
    raw = _make_zip({"SKILL.md": SKILL_MD, "scripts/greet.py": b"print('hi')"})
    parsed = validate_and_parse(raw)
    assert parsed.skill_name == "greet"
    assert parsed.skill_description == "Greets the user by name."
    assert len(parsed.checksum) == 64
    assert parsed.size_bytes == len(raw)
    assert "Run scripts/greet.py" in parsed.skill_md_body


def test_missing_skill_md_rejected():
    raw = _make_zip({"scripts/greet.py": b"print('hi')"})
    with pytest.raises(SkillBundleError, match="SKILL.md"):
        validate_and_parse(raw)


def test_zip_slip_rejected():
    raw = _make_zip({"SKILL.md": SKILL_MD, "../../etc/passwd": b"pwned"})
    with pytest.raises(SkillBundleError, match="Unsafe path"):
        validate_and_parse(raw)


def test_absolute_path_rejected():
    raw = _make_zip({"SKILL.md": SKILL_MD, "/etc/passwd": b"pwned"})
    with pytest.raises(SkillBundleError, match="Unsafe path"):
        validate_and_parse(raw)


def test_symlink_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        link_info = zipfile.ZipInfo("scripts/evil-link")
        link_info.external_attr = (0o120777 & 0xFFFF) << 16
        zf.writestr(link_info, "/etc/passwd")
    with pytest.raises(SkillBundleError, match="Symlinks"):
        validate_and_parse(buf.getvalue())


def test_bad_zip_rejected():
    with pytest.raises(SkillBundleError, match="not a valid zip"):
        validate_and_parse(b"this is not a zip file")


def test_missing_name_in_frontmatter_rejected():
    bad_md = b"---\ndescription: no name here\n---\nbody"
    raw = _make_zip({"SKILL.md": bad_md})
    with pytest.raises(SkillBundleError, match="name"):
        validate_and_parse(raw)


def test_missing_description_in_frontmatter_rejected():
    bad_md = b"---\nname: greet\n---\nbody"
    raw = _make_zip({"SKILL.md": bad_md})
    with pytest.raises(SkillBundleError, match="description"):
        validate_and_parse(raw)


def test_oversized_bundle_rejected(monkeypatch):
    from src.core.config import settings

    monkeypatch.setattr(settings, "SKILL_BUNDLE_MAX_BYTES", 10)
    raw = _make_zip({"SKILL.md": SKILL_MD})
    with pytest.raises(SkillBundleError, match="exceeds max size"):
        validate_and_parse(raw)


def test_too_many_entries_rejected(monkeypatch):
    from src.core.config import settings

    monkeypatch.setattr(settings, "SKILL_BUNDLE_MAX_ENTRIES", 2)
    raw = _make_zip({"SKILL.md": SKILL_MD, "a.txt": b"1", "b.txt": b"2", "c.txt": b"3"})
    with pytest.raises(SkillBundleError, match="too many entries"):
        validate_and_parse(raw)


def test_zip_bomb_ratio_rejected(monkeypatch):
    from src.core.config import settings

    monkeypatch.setattr(settings, "SKILL_BUNDLE_MAX_COMPRESSION_RATIO", 2)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("SKILL.md", SKILL_MD)
        # Highly compressible payload so uncompressed:compressed ratio is huge.
        zf.writestr("scripts/bomb.bin", b"0" * 1_000_000)
    with pytest.raises(SkillBundleError, match="compression ratio"):
        validate_and_parse(buf.getvalue())
