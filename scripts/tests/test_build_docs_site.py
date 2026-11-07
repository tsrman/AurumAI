"""Pins scripts/build-docs-site.py: the pages, their links and the navigation."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-docs-site.py"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv runs the script with its own dependency")


def build(source: Path, output: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--script", str(SCRIPT), "--source", str(source), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_page_gets_ids_tables_code_links_and_a_marked_navigation_entry(tmp_path):
    source, output = tmp_path / "docs", tmp_path / "site"
    write(source / "README.md", "# Home\n\nSee [the guide](guides/user-guide.md#sign-in).\n")
    write(
        source / "guides" / "user-guide.md",
        "# User guide\n\n## Sign in\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        '```json\n{"k": "it\'s"}\n```\n\n'
        "```mermaid\nflowchart LR\n  A --> B\n```\n\n"
        "[home](../README.md), [source](../../charts/aurum-ai/README.md#values), "
        "[site](https://example.org/x.md)\n\n![shot](../assets/screenshots/a.png)\n",
    )
    write(source / "assets" / "screenshots" / "a.png", "png")
    write(source / "superpowers" / "plans" / "p.md", "# private plan\n")

    result = build(source, output)
    assert result.returncode == 0, result.stderr

    assert sorted(p.relative_to(output).as_posix() for p in output.rglob("*.html")) == [
        "guides/user-guide.html",
        "index.html",
    ]
    assert (output / "assets" / "screenshots" / "a.png").exists()

    guide = (output / "guides" / "user-guide.html").read_text()
    assert "<title>User guide · AurumAI</title>" in guide
    assert '<h2 id="sign-in">Sign in</h2>' in guide
    assert '<div class="tw"><table>' in guide
    assert '<pre><code class="language-json">{&quot;k&quot;: &quot;it\'s&quot;}' in guide
    assert '<pre class="mermaid">flowchart LR' in guide and "mermaid.esm.min.mjs" in guide
    assert 'href="../index.html">home' in guide
    assert 'href="https://github.com/tsrman/AurumAI/blob/main/charts/aurum-ai/README.md#values"' in guide
    assert 'href="https://example.org/x.md"' in guide
    assert 'src="../assets/screenshots/a.png"' in guide
    # The navigation is relative to the page, and marks the page's own entry.
    assert '<a href="../guides/user-guide.html" aria-current="page">User guide</a>' in guide
    assert guide.count('aria-current="page"') == 1

    index = (output / "index.html").read_text()
    assert 'href="guides/user-guide.html#sign-in"' in index
    assert '<a href="guides/user-guide.html">User guide</a>' in index
    assert 'aria-current="page"' not in index
    assert "mermaid.esm.min.mjs" not in index


def test_it_refuses_a_directory_that_already_has_content(tmp_path):
    source, output = tmp_path / "docs", tmp_path / "site"
    write(source / "README.md", "# Home\n")
    write(output / "keep.txt", "x")
    result = build(source, output)
    assert result.returncode == 2
    assert (output / "keep.txt").exists()


def test_the_real_documentation_builds_without_a_broken_local_link(tmp_path):
    output = tmp_path / "site"
    result = build(ROOT / "docs", output)
    assert result.returncode == 0, result.stderr

    pages = sorted(output.rglob("*.html"))
    assert len(pages) >= 20
    broken = []
    for page in pages:
        text = page.read_text()
        ids = set(re.findall(r'\sid="([^"]+)"', text))
        for target in re.findall(r'(?:href|src)="([^"]+)"', text):
            if re.match(r"^(https?:|mailto:)", target):
                continue
            path, _, anchor = target.partition("#")
            destination = page if not path else (page.parent / path).resolve()
            if not destination.exists():
                broken.append(f"{page.relative_to(output)} -> {target}")
            elif anchor and destination.suffix == ".html":
                known = ids if destination == page else set(re.findall(r'\sid="([^"]+)"', destination.read_text()))
                if anchor not in known:
                    broken.append(f"{page.relative_to(output)} -> {target} (no such heading)")
    assert not broken, "\n".join(broken)
