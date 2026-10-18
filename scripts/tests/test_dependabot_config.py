from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def updates():
    return yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]


def entry(ecosystem):
    [found] = [u for u in updates() if u["package-ecosystem"] == ecosystem]
    return found


def ignored(ecosystem):
    return {rule["dependency-name"]: rule.get("versions") for rule in entry(ecosystem).get("ignore", [])}


def test_every_manifest_directory_is_watched():
    lockfiles = {"/" + p.parent.relative_to(ROOT).as_posix() for p in ROOT.glob("*/uv.lock")}
    packages = {
        "/" + p.parent.relative_to(ROOT).as_posix()
        for p in ROOT.glob("*/package-lock.json")
    }
    dockerfiles = {
        "/" + p.parent.relative_to(ROOT).as_posix()
        for p in ROOT.glob("**/Dockerfile")
        if "node_modules" not in p.parts and ".venv" not in p.parts and p.parent != ROOT / "scripts"
    }
    assert set(entry("uv")["directories"]) == lockfiles
    assert set(entry("npm")["directories"]) == packages
    assert set(entry("docker")["directories"]) == dockerfiles
    assert entry("github-actions")["directories"] == ["/"]


def test_patch_and_minor_updates_are_grouped_and_open_prs_are_bounded():
    for update in updates():
        groups = update["groups"]
        assert any(set(g.get("update-types", [])) == {"minor", "patch"} for g in groups.values())
        assert 0 < update["open-pull-requests-limit"] <= 5


def test_known_incompatible_major_versions_are_ignored():
    assert ignored("uv")["ruff"] is None
    assert ignored("uv")["langfuse"] == [">=3"]
    assert ignored("npm")["typescript"] == [">=7"]
    assert ignored("npm")["ky"] == [">=2"]
    assert ignored("docker")["python"] == [">=3.13"]
