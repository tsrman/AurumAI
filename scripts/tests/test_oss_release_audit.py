from __future__ import annotations

import importlib
import json
import sys
import zipfile
from pathlib import Path


def audit_module():
    scripts = Path(__file__).resolve().parents[1]
    assert (scripts / "oss_release/audit.py").is_file(), "Release audit is not implemented"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module("oss_release.audit")


def release_manifest():
    from oss_release.export import ReleaseManifest
    return ReleaseManifest(("README.md", "app/**", "skills/**"), (), (), ("README.md",))


def test_secret_diagnostics_never_contain_the_value(tmp_path: Path):
    audit = audit_module()
    sentinel = "ghp_" + "z" * 36
    (tmp_path / "README.md").write_text(sentinel)
    findings = audit.audit_tree(tmp_path, release_manifest())
    assert [f.rule for f in findings] == ["github_token"]
    assert sentinel not in json.dumps([vars(f) for f in findings])


def test_archive_members_receive_the_same_secret_scan(tmp_path: Path):
    audit = audit_module()
    (tmp_path / "README.md").write_text("# AurumAI")
    (tmp_path / "skills").mkdir()
    with zipfile.ZipFile(tmp_path / "skills/example.zip", "w") as archive:
        archive.writestr("scripts/main.py", "ghp_" + "x" * 36)
    findings = audit.audit_tree(tmp_path, release_manifest())
    assert [(f.path, f.rule) for f in findings] == [
        ("skills/example.zip!scripts/main.py", "github_token")
    ]


def test_traversal_archive_is_rejected_without_extracting(tmp_path: Path):
    audit = audit_module()
    (tmp_path / "README.md").write_text("# AurumAI")
    (tmp_path / "skills").mkdir()
    with zipfile.ZipFile(tmp_path / "skills/example.zip", "w") as archive:
        archive.writestr("../../escape", "unsafe")
    assert "unsafe_archive_path" in [f.rule for f in audit.audit_tree(tmp_path, release_manifest())]
    assert not (tmp_path.parent / "escape").exists()


def test_unknown_files_env_and_symlinks_are_release_blockers(tmp_path: Path):
    audit = audit_module()
    (tmp_path / "README.md").write_text("# AurumAI")
    (tmp_path / ".ENV").write_text("secret")
    (tmp_path / "unreviewed.json").write_text("{}")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/link.py").symlink_to(tmp_path / "README.md")
    rules = {f.rule for f in audit.audit_tree(tmp_path, release_manifest())}
    assert rules == {"prohibited_path", "unselected_file", "unsafe_source_file"}


def test_archive_expansion_is_bounded(tmp_path: Path, monkeypatch):
    audit = audit_module()
    monkeypatch.setattr(audit, "MAX_ARCHIVE_BYTES", 32)
    (tmp_path / "README.md").write_text("# AurumAI")
    (tmp_path / "skills").mkdir()
    with zipfile.ZipFile(tmp_path / "skills/example.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.txt", "x" * 100)
    assert "archive_limit" in [f.rule for f in audit.audit_tree(tmp_path, release_manifest())]


def test_operator_paths_and_account_ids_are_redacted(tmp_path: Path):
    audit = audit_module()
    (tmp_path / "README.md").write_text("/home/" + "private-owner/notes\n" + "b1g" + "a" * 16)
    findings = audit.audit_tree(tmp_path, release_manifest())
    assert {f.rule for f in findings} == {"personal_path", "cloud_account"}
    assert "private-owner" not in repr(findings)


def test_temporal_json_payloads_are_scanned_after_base64_decoding(tmp_path: Path):
    import base64
    audit = audit_module()
    token = "ghp_" + "x" * 36
    payload = {"metadata": {"encoding": base64.b64encode(b"json/plain").decode()},
        "data": base64.b64encode(json.dumps({"token": token}).encode()).decode()}
    (tmp_path / "README.md").write_text("# AurumAI")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/history.json").write_text(json.dumps({"events": [{"payloads": [payload]}]}))
    findings = audit.audit_tree(tmp_path, release_manifest())
    assert any(f.rule == "github_token" for f in findings)
    assert token not in repr(findings)


def test_package_artifact_hash_is_not_a_cloud_account(tmp_path: Path):
    audit = audit_module()
    artifact = "https://files.pythonhosted.org/packages/aa/bb/" + "f8d" + "a" * 61 + "/package.whl"
    (tmp_path / "README.md").write_text(artifact)
    assert audit.audit_tree(tmp_path, release_manifest()) == ()
