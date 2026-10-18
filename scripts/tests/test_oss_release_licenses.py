from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def collector():
    scripts = Path(__file__).resolve().parents[1]
    assert (scripts / "oss_release/licenses.py").is_file(), "Dependency attribution is not implemented"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module("oss_release.licenses")


def test_exact_lock_version_controls_metadata_admission(tmp_path: Path):
    service = tmp_path / "backend"
    service.mkdir()
    (service / "uv.lock").write_text('version = 1\n[[package]]\nname = "example"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n')
    metadata = service / ".venv/lib/python3.12/site-packages/example-2.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: example\nVersion: 2.0\nLicense-Expression: MIT\n")
    inventory = collector().collect_dependency_inventory(tmp_path)
    assert [(r.name, r.version, r.license) for r in inventory.records] == [("example", "1.0", "UNKNOWN")]
    assert len(inventory.findings) == 1


def test_dependency_keeps_its_own_license_and_relative_evidence(tmp_path: Path):
    service = tmp_path / "backend"
    service.mkdir()
    (service / "uv.lock").write_text('version = 1\n[[package]]\nname = "example"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n')
    metadata = service / ".venv/lib/python3.12/site-packages/example-1.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: example\nVersion: 1.0\nLicense-Expression: Apache-2.0\n")
    inventory = collector().collect_dependency_inventory(tmp_path)
    assert [(r.name, r.version, r.license) for r in inventory.records] == [("example", "1.0", "Apache-2.0")]
    assert not inventory.findings
    assert str(tmp_path) not in json.dumps(inventory.to_dict())


def test_missing_npm_field_can_use_the_exact_distribution_notice(tmp_path: Path):
    app = tmp_path / "ui"
    app.mkdir()
    (app / "package-lock.json").write_text(json.dumps({"packages": {"": {"name": "own-app"}, "node_modules/example": {"version": "1.2.3"}}}))
    package = app / "node_modules/example"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "example", "version": "1.2.3"}))
    (package / "license").write_text("MIT License\nCopyright Example Authors\nPermission is hereby granted, free of charge\n")
    inventory = collector().collect_dependency_inventory(tmp_path)
    assert [(r.name, r.version, r.license) for r in inventory.records] == [("example", "1.2.3", "MIT")]
    assert not inventory.findings


def test_root_owned_package_is_not_reported_as_a_third_party_dependency(tmp_path: Path):
    (tmp_path / "package-lock.json").write_text(json.dumps({"packages": {"": {"name": "aurumai"}}}))
    assert collector().collect_dependency_inventory(tmp_path).records == ()
