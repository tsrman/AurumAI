from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def updates():
    return yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())["updates"]


def entries(ecosystem):
    return [u for u in updates() if u["package-ecosystem"] == ecosystem]


def entry(ecosystem):
    [found] = entries(ecosystem)
    return found


def watched(ecosystem):
    directories = [d for u in entries(ecosystem) for d in u["directories"]]
    assert len(directories) == len(set(directories)), f"{ecosystem} directories overlap"
    return set(directories)


def ignore_rules(update):
    return {rule["dependency-name"]: rule.get("versions") for rule in update.get("ignore", [])}


def ignored(ecosystem):
    rules = [ignore_rules(u) for u in entries(ecosystem)]
    assert all(r == rules[0] for r in rules), f"{ecosystem} entries disagree on ignore rules"
    return rules[0]


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
    assert watched("uv") == lockfiles
    assert set(entry("npm")["directories"]) == packages
    assert set(entry("docker")["directories"]) == dockerfiles
    assert entry("github-actions")["directories"] == ["/"]


def test_rag_worker_python_updates_run_in_their_own_entry():
    """rag-worker's torch/docling resolution alone can exhaust Dependabot's job
    time; sharing a run with the other services cancelled the whole update."""
    rag_worker = [u for u in entries("uv") if "/rag-worker" in u["directories"]]
    assert len(rag_worker) == 1 and rag_worker[0]["directories"] == ["/rag-worker"]
    group_names = [name for u in entries("uv") for name in u["groups"]]
    assert len(group_names) == len(set(group_names))


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
