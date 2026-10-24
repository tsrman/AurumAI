from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


def implementation():
    scripts = Path(__file__).resolve().parents[1]
    assert (scripts / "oss_release/export.py").is_file(), "OSS exporter is not implemented"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module("oss_release.export")


def source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    return source


def track(source: Path, name: str, content: str = "public source\n") -> None:
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "-C", str(source), "add", "--", name], check=True)


def manifest(source: Path, **overrides):
    data = {
        "schema_version": 1,
        "include": ["README.md", "app/**", "scripts/**", ".env.example"],
        "exclude": ["dev_log.md", "docs/superpowers/**", ".superdesign/**"],
        "new_files": [".env.example"],
        "required": ["README.md", "app/main.py", ".env.example"],
    }
    data.update(overrides)
    path = source / "manifest.json"
    path.write_text(json.dumps(data))
    return implementation().load_manifest(path)


def ready_source(tmp_path: Path) -> Path:
    source = source_repo(tmp_path)
    track(source, "README.md", "# AurumAI\n")
    track(source, "app/main.py", "print('hello')\n")
    (source / ".env.example").write_text("APP_DEBUG=true\n")
    return source


def test_snapshot_uses_reviewed_paths_and_never_walks_local_data(tmp_path: Path):
    source = ready_source(tmp_path)
    track(source, "dev_log.md", "private history\n")
    track(source, ".superdesign/resume.json", "{}")
    (source / ".env").write_text("PRIVATE_SECRET=never-copy\n")
    (source / "app/local.json").write_text("private local data")

    selected = implementation().select_files(source, manifest(source))

    assert selected == (".env.example", "README.md", "app/main.py")


def test_export_is_deterministic_and_keeps_executable_mode(tmp_path: Path):
    source = ready_source(tmp_path)
    track(source, "scripts/run.sh", "#!/bin/sh\nexit 0\n")
    (source / "scripts/run.sh").chmod(0o755)
    export = implementation()
    first = export.export_candidate(source, tmp_path / "first", manifest(source))
    second = export.export_candidate(source, tmp_path / "second", manifest(source))

    assert first.records == second.records
    assert first.paths == (".env.example", "README.md", "app/main.py", "scripts/run.sh")
    assert (tmp_path / "first/scripts/run.sh").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "first/README.md").read_text() == "# AurumAI\n"
    assert not (tmp_path / "first/.git").exists()


def test_existing_destination_is_never_changed(tmp_path: Path):
    source = ready_source(tmp_path)
    destination = tmp_path / "candidate"
    destination.mkdir()
    (destination / "keep").write_text("existing candidate")

    with pytest.raises(FileExistsError):
        implementation().export_candidate(source, destination, manifest(source))

    assert (destination / "keep").read_text() == "existing candidate"
    assert list(destination.iterdir()) == [destination / "keep"]


def test_racing_empty_destination_is_never_replaced(tmp_path: Path, monkeypatch):
    source = ready_source(tmp_path)
    destination = tmp_path / "candidate"
    exists = Path.exists
    calls = 0

    def race(path):
        nonlocal calls
        result = exists(path)
        if path == destination:
            calls += 1
            if calls == 2:
                destination.mkdir()
        return result

    monkeypatch.setattr(Path, "exists", race)
    with pytest.raises(FileExistsError):
        implementation().export_candidate(source, destination, manifest(source))
    assert list(destination.iterdir()) == []


def test_broken_selected_symlink_is_rejected(tmp_path: Path):
    source = ready_source(tmp_path)
    (source / "app/broken.py").symlink_to(tmp_path / "missing")
    subprocess.run(["git", "-C", str(source), "add", "app/broken.py"], check=True)
    with pytest.raises(ValueError, match="unsafe_source_file"):
        implementation().export_candidate(source, tmp_path / "candidate", manifest(source))


@pytest.mark.parametrize("unsafe", ["../outside", "/outside", "app/../outside", "app\\outside"])
def test_manifest_rejects_unsafe_paths(tmp_path: Path, unsafe: str):
    source = ready_source(tmp_path)
    with pytest.raises(ValueError, match="unsafe_manifest_path"):
        manifest(source, new_files=[unsafe])


def test_manifest_rejects_unknown_version(tmp_path: Path):
    source = ready_source(tmp_path)
    with pytest.raises(ValueError, match="unsupported_manifest_version"):
        manifest(source, schema_version=2)


def test_missing_required_file_leaves_no_candidate(tmp_path: Path):
    source = ready_source(tmp_path)
    destination = tmp_path / "candidate"
    with pytest.raises(ValueError, match="missing_required_file"):
        implementation().export_candidate(
            source, destination, manifest(source, required=["LICENSE"])
        )
    assert not destination.exists()


def test_selected_symlink_cannot_export_data_outside_source(tmp_path: Path):
    source = ready_source(tmp_path)
    outside = tmp_path / "private"
    outside.write_text("private contents")
    (source / "app/secret.py").symlink_to(outside)
    subprocess.run(["git", "-C", str(source), "add", "app/secret.py"], check=True)
    destination = tmp_path / "candidate"

    with pytest.raises(ValueError, match="unsafe_source_file"):
        implementation().export_candidate(source, destination, manifest(source))
    assert not destination.exists()
    assert outside.read_text() == "private contents"


def test_broad_include_still_excludes_prohibited_artifacts(tmp_path: Path):
    source = ready_source(tmp_path)
    track(source, ".env", "LOCAL_SECRET=private")
    track(source, "app/.env.local", "LOCAL_SECRET=private")
    track(source, ".superdesign/private.json", "{}")
    track(source, "review.md", "private incident")

    selected = implementation().select_files(
        source, manifest(source, include=["**"], exclude=[])
    )
    assert selected == (".env.example", "README.md", "app/main.py")


def test_public_candidate_ships_executable_platform_installer(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    exporter = implementation()
    candidate = tmp_path / "candidate"
    exporter.export_candidate(root, candidate, exporter.load_manifest(root / "release/oss-manifest.json"))

    installer = candidate / "install-platform.sh"
    assert installer.is_file(), "The documented installation entry point is missing from the release"
    result = subprocess.run([str(installer), "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "Usage: ./install-platform.sh" in result.stdout
