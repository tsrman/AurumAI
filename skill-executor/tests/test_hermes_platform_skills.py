"""Platform code skills packed for a Hermes sandbox: verified, bounded, and
placed only under the staging directory."""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile

import pytest

from src import hermes_platform_skills as platform
from src.hermes_platform_skills import PlatformSkill, staging_tar

STAGING = ".aurum/hydrate/abc"


def _bundle(files: dict[str, bytes], *, executable: tuple[str, ...] = (), link: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100755 if name in executable else 0o100644) << 16
            archive.writestr(info, data)
        if link:
            info = zipfile.ZipInfo(link)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


def _skill(directory: str, bundle: bytes, checksum: str | None = None) -> PlatformSkill:
    return PlatformSkill(directory, f"skills/{directory}.zip", checksum or hashlib.sha256(bundle).hexdigest())


async def _pack(bundles: dict[str, bytes], skills: list[PlatformSkill]) -> tuple[dict[str, tarfile.TarInfo], int, int]:
    async def fetch(key: str) -> bytes:
        return bundles[key]

    archive, hydrated, skipped = await staging_tar(skills, fetch, STAGING)
    with tarfile.open(fileobj=io.BytesIO(archive)) as out:
        return {info.name: info for info in out.getmembers()}, hydrated, skipped


@pytest.mark.asyncio
async def test_a_verified_bundle_is_unpacked_under_its_directory_for_the_sandbox_user() -> None:
    bundle = _bundle({"SKILL.md": b"---\nname: s\n---\n", "./scripts/run.sh": b"echo"}, executable=("./scripts/run.sh",))

    members, hydrated, skipped = await _pack({"skills/standup-1a.zip": bundle}, [_skill("standup-1a", bundle)])

    assert (hydrated, skipped) == (1, 0)
    assert set(members) == {
        ".aurum/hydrate",
        STAGING,
        f"{STAGING}/standup-1a",
        f"{STAGING}/standup-1a/scripts",
        f"{STAGING}/standup-1a/SKILL.md",
        f"{STAGING}/standup-1a/scripts/run.sh",
    }
    assert {(info.uid, info.gid) for info in members.values()} == {(10001, 10001)}
    assert members[f"{STAGING}/standup-1a/scripts/run.sh"].mode == 0o755
    assert members[f"{STAGING}/standup-1a/SKILL.md"].mode == 0o644
    assert not any(info.issym() or info.islnk() for info in members.values())


@pytest.mark.asyncio
async def test_bundles_that_do_not_verify_or_fit_are_left_out(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _bundle({"SKILL.md": b"ok"})
    linked = _bundle({"SKILL.md": b"ok"}, link="passwd")
    escaping = _bundle({"SKILL.md": b"ok", "../outside": b"x"})
    no_skill = _bundle({"README.md": b"x"})
    big = _bundle({"SKILL.md": b"ok", "data.bin": bytes(range(256)) * 400})
    bundles = {f"skills/{name}.zip": data for name, data in
               {"good": good, "linked": linked, "escaping": escaping, "none": no_skill, "big": big, "tampered": good}.items()}
    monkeypatch.setattr(platform, "MAX_TOTAL_BYTES", 50_000)
    skills = [
        _skill("good", good),
        _skill("linked", linked),
        _skill("escaping", escaping),
        _skill("none", no_skill),
        _skill("big", big),
        _skill("tampered", good, "0" * 64),
        _skill("missing", good),
        _skill("Bad_Dir", good),
        _skill("good", good),
    ]

    members, hydrated, skipped = await _pack(bundles, skills)

    assert (hydrated, skipped) == (1, 8)
    assert {name.split("/")[3] for name in members if name.count("/") >= 3} == {"good"}


@pytest.mark.asyncio
async def test_no_skills_still_stage_an_empty_directory() -> None:
    members, hydrated, skipped = await _pack({}, [])

    assert set(members) == {".aurum/hydrate", STAGING} and (hydrated, skipped) == (0, 0)
