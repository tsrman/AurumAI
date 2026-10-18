import importlib
import sys
from pathlib import Path


def checker():
    scripts = Path(__file__).resolve().parents[1]
    assert (scripts / "oss_release/docs.py").is_file(), "Public documentation link checker is not implemented"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module("oss_release.docs")


def test_missing_file_and_heading_are_reported_without_publishing_link_values(tmp_path: Path):
    (tmp_path / "README.md").write_text("[Missing](missing.md)\n[Heading](guide.md#not-there)\n")
    (tmp_path / "guide.md").write_text("# Actual heading\n")
    findings = checker().check_links(tmp_path, ("README.md", "guide.md"))
    assert [f.rule for f in findings] == ["missing_document_link", "missing_document_anchor"]


def test_existing_relative_links_and_code_examples_pass(tmp_path: Path):
    (tmp_path / "README.md").write_text("[Guide](guide.md#actual-heading)\n```text\n[Example](not-a-real-file)\n```\n")
    (tmp_path / "guide.md").write_text("# Actual heading\n")
    assert checker().check_links(tmp_path, ("README.md", "guide.md")) == ()


def test_document_link_cannot_escape_the_candidate(tmp_path: Path):
    (tmp_path / "README.md").write_text("[Outside](../private.md)\n")
    assert [f.rule for f in checker().check_links(tmp_path, ("README.md",))] == ["unsafe_document_link"]
