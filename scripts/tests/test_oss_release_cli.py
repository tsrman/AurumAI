from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


CLI = Path(__file__).resolve().parents[1] / "prepare-oss-release.py"


def fixture(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    readme = candidate / "README.md"
    readme.write_text("# AurumAI\n")
    readme.chmod(0o644)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "include": ["README.md"],
        "exclude": [], "new_files": [], "required": ["README.md"]}))
    import hashlib
    record = {"path": "README.md", "sha256": hashlib.sha256(readme.read_bytes()).hexdigest(),
        "size": readme.stat().st_size, "mode": 0o644}
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"schema_version": 1, "files": [record]}))
    return candidate, manifest, inventory, record


def verify(candidate, manifest, inventory, report):
    return subprocess.run([sys.executable, str(CLI), "verify", "--candidate", str(candidate),
        "--manifest", str(manifest), "--inventory", str(inventory), "--report", str(report)],
        capture_output=True, text=True)


def test_verify_accepts_unchanged_candidate_and_writes_private_report(tmp_path: Path):
    candidate, manifest, inventory, _ = fixture(tmp_path)
    report = tmp_path / "report.json"
    assert verify(candidate, manifest, inventory, report).returncode == 0
    assert report.stat().st_mode & 0o777 == 0o600


def test_verify_detects_changed_executable_mode(tmp_path: Path):
    candidate, manifest, inventory, _ = fixture(tmp_path)
    (candidate / "README.md").chmod(0o755)
    report = tmp_path / "report.json"
    assert verify(candidate, manifest, inventory, report).returncode == 1
    assert "inventory_mode_changed" in report.read_text()


def test_verify_rejects_report_inside_candidate_before_mutation(tmp_path: Path):
    candidate, manifest, inventory, _ = fixture(tmp_path)
    report = candidate / "report.json"
    assert verify(candidate, manifest, inventory, report).returncode == 1
    assert not report.exists()


@pytest.mark.parametrize("change", ["duplicate", "unsafe", "schema", "digest", "size"])
def test_verify_rejects_invalid_inventory(tmp_path: Path, change: str):
    candidate, manifest, inventory, record = fixture(tmp_path)
    data = {"schema_version": 1, "files": [record]}
    if change == "duplicate":
        data["files"].append(record)
    elif change == "unsafe":
        record["path"] = "../outside"
    elif change == "schema":
        data["schema_version"] = 99
    elif change == "digest":
        record["sha256"] = "invalid"
    else:
        record["size"] = -1
    inventory.write_text(json.dumps(data))
    assert verify(candidate, manifest, inventory, tmp_path / "report.json").returncode == 1


def test_verify_accepts_next_dynamic_route_filenames(tmp_path: Path):
    candidate, manifest, inventory, _ = fixture(tmp_path)
    route = candidate / "app/[id]/page.tsx"
    route.parent.mkdir(parents=True)
    route.write_text('export default function Page() { return null; }\n')
    route.chmod(0o644)
    data = json.loads(manifest.read_text())
    data['include'].append('app/**')
    manifest.write_text(json.dumps(data))
    import hashlib
    data = json.loads(inventory.read_text())
    data['files'].append({'path': 'app/[id]/page.tsx',
        'sha256': hashlib.sha256(route.read_bytes()).hexdigest(), 'size': route.stat().st_size, 'mode': 0o644})
    inventory.write_text(json.dumps(data))
    assert verify(candidate, manifest, inventory, tmp_path / 'report.json').returncode == 0
