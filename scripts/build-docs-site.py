#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["markdown-it-py==3.0.0"]
# ///
"""Builds the documentation site from docs/*.md.

    uv run --script scripts/build-docs-site.py --source docs --output /tmp/site

Every Markdown page becomes an HTML page of the same name (docs/README.md becomes
index.html) in a plain template with a navigation bar; docs/assets is copied as is.
Links between pages are rewritten from .md to .html, links that leave docs/ point at
the source on GitHub, and every heading gets an id. ```mermaid blocks are drawn in the
browser. docs/superpowers (design notes) is not published. The site is published to
https://tsrman.github.io/aurumai/ by .github/workflows/publish-docs.yml.
"""
import argparse
import html
import posixpath
import re
import shutil
import sys
from pathlib import Path

from markdown_it import MarkdownIt

SOURCE_URL = "https://github.com/tsrman/AurumAI"
SKIPPED = ("assets", "superpowers")

# Which navigation entry marks a page as "current" (pages without an entry mark none).
CURRENT = {
    "guides/user-guide.md": "guides/user-guide.html",
    "guides/admin-guide.md": "guides/admin-guide.html",
    "WORKFLOWS.md": "guides/admin-guide.html",
    "STARTER_TOOLS.md": "guides/admin-guide.html",
    "AGENT_ROLES.md": "guides/admin-guide.html",
    "guides/hermes-agent.md": "guides/hermes-agent.html",
    "HERMES_AGENT.md": "guides/hermes-agent.html",
    "guides/deployment.md": "guides/deployment.html",
    "CONFIGURATION.md": "guides/deployment.html",
    "AIR_GAPPED.md": "guides/deployment.html",
    "guides/operations.md": "guides/operations.html",
    "operations/data-archive.md": "guides/operations.html",
    "operations/secrets.md": "guides/operations.html",
    "operations/backup-recovery.md": "guides/operations.html",
    "operations/network-enforcement.md": "guides/operations.html",
    "operations/model-changes.md": "guides/operations.html",
    "reference/api.md": "reference/api.html",
    "reference/security.md": "reference/security.html",
}

HEAD_BEFORE = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
HEAD_AFTER = '\n<meta name="description" content="AurumAI documentation: self-hosted AI chat and agent platform.">\n<style>\n:root{--bg:#fff;--fg:#1b1f27;--muted:#5d6675;--line:#e3e6ec;--soft:#f5f6f9;--accent:#b97a05;--code:#f1f3f7}\n@media (prefers-color-scheme:dark){:root{--bg:#14161b;--fg:#e6e8ee;--muted:#98a1b3;--line:#2a2f3a;--soft:#1b1e25;--accent:#f0b441;--code:#1f232b}}\n*{box-sizing:border-box}\nbody{margin:0;background:var(--bg);color:var(--fg);font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}\nheader{border-bottom:1px solid var(--line);background:var(--soft)}\n.bar{max-width:980px;margin:0 auto;padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center}\n.bar a{color:var(--fg);text-decoration:none;font-size:14px}\n.bar a.brand{font-weight:700;font-size:16px;color:var(--accent)}\n.bar a:hover{text-decoration:underline}\n.bar a[aria-current]{color:var(--accent);font-weight:600}\nmain{max-width:980px;margin:0 auto;padding:8px 16px 56px}\nh1,h2,h3{line-height:1.25} h1{margin-top:28px} h2{margin-top:36px;border-bottom:1px solid var(--line);padding-bottom:6px}\na{color:var(--accent)}\nimg{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;display:block;margin:12px 0}\ncode{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.9em}\npre{background:var(--code);padding:12px 14px;border-radius:8px;overflow:auto} pre code{background:none;padding:0}\n.tw{overflow-x:auto} table{border-collapse:collapse;margin:14px 0;width:100%}\nth,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top;font-size:15px} th{background:var(--soft)}\nblockquote{margin:12px 0;padding:2px 14px;border-left:4px solid var(--accent);background:var(--soft);color:var(--muted)}\nfooter{max-width:980px;margin:0 auto;padding:16px;border-top:1px solid var(--line);color:var(--muted);font-size:14px}\npre.mermaid{background:none;text-align:center}\n</style>\n</head>\n<body>\n'
NAV = '<header><div class="bar">\n<a class="brand" href="{P}index.html">AurumAI docs</a>\n<a href="{P}guides/user-guide.html">User guide</a>\n<a href="{P}guides/admin-guide.html">Administrator</a>\n<a href="{P}guides/hermes-agent.html">Hermes Agent</a>\n<a href="{P}guides/deployment.html">Installation</a>\n<a href="{P}guides/operations.html">Operations</a>\n<a href="{P}reference/api.html">API</a>\n<a href="{P}reference/security.html">Security</a>\n<a href="https://github.com/tsrman/AurumAI">Source</a>\n</div></header>\n'
FOOT = '<footer>AurumAI is free software under AGPL-3.0-only. <a href="https://github.com/tsrman/AurumAI">Source code</a> · <a href="https://github.com/tsrman/AurumAI/blob/main/LICENSE">License</a></footer>\n'
MERMAID = '<script type="module">import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@12.3.0/dist/mermaid.esm.min.mjs";mermaid.initialize({startOnLoad:true,theme:window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"default"});</script>'


def slug(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def render(markdown: str, relpath: str) -> str:
    """The HTML page for the Markdown source of docs/<relpath>."""
    md = MarkdownIt("commonmark", {"xhtmlOut": True}).enable("table")
    tokens = md.parse(markdown)
    seen: dict[str, int] = {}
    title = None
    mermaid = False
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[index + 1]
            base = slug(inline.content)
            count = seen.get(base, 0)
            seen[base] = count + 1
            token.attrSet("id", base if count == 0 else f"{base}-{count}")
            if token.tag == "h1" and title is None:
                title = inline.content
        if token.type == "inline" and token.children:
            for child in token.children:
                if child.type == "link_open":
                    child.attrSet("href", rewrite(child.attrGet("href") or "", relpath))

    def fence(toks, i, options, env):
        nonlocal mermaid
        token = toks[i]
        lang = (token.info or "").strip().split(" ")[0]
        if lang == "mermaid":
            mermaid = True
            return f'<pre class="mermaid">{html.escape(token.content, quote=False)}</pre>\n'
        css = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{css}>{html.escape(token.content).replace('&#x27;', chr(39))}</code></pre>\n"

    md.renderer.rules["fence"] = fence
    md.renderer.rules["table_open"] = lambda toks, i, o, e: '<div class="tw"><table>\n'
    md.renderer.rules["table_close"] = lambda toks, i, o, e: "</table></div>\n"
    body = md.renderer.render(tokens, md.options, {})

    head = HEAD_AFTER
    if "aria-current" not in head:
        head = head.replace(
            ".bar a:hover{text-decoration:underline}",
            ".bar a:hover{text-decoration:underline}\n.bar a[aria-current]{color:var(--accent);font-weight:600}",
        )
    nav = NAV
    current = CURRENT.get(relpath)
    if current:
        nav = nav.replace(f'<a href="{{P}}{current}">', f'<a href="{{P}}{current}" aria-current="page">')
    nav = nav.replace("{P}", "../" * relpath.count("/"))
    script = (MERMAID + "\n") if mermaid else "\n"
    return (
        HEAD_BEFORE + "<title>" + html.escape(title or "AurumAI", quote=False) + " · AurumAI</title>"
        + head + nav + "<main>\n" + body + "\n</main>\n" + FOOT + script + "</body>\n</html>\n"
    )


def rewrite(href: str, relpath: str) -> str:
    if not href or re.match(r"^(https?:|#|mailto:)", href):
        return href
    path, _, anchor = href.partition("#")
    anchor = "#" + anchor if anchor else ""
    folder = posixpath.dirname(relpath)
    if posixpath.normpath(posixpath.join(folder, path)).startswith(".."):
        repo_path = posixpath.normpath(posixpath.join("docs", folder, path))
        kind = "blob" if repo_path.endswith(".md") else "tree"
        return f"{SOURCE_URL}/{kind}/main/{repo_path}{anchor}"
    if posixpath.normpath(posixpath.join(folder, path)) == "README.md":
        # docs/README.md is the site's front page.
        return path[: -len("README.md")] + "index.html" + anchor
    if path.endswith(".md"):
        return path[: -len(".md")] + ".html" + anchor
    return href


def pages(source: Path):
    for path in sorted(source.rglob("*.md")):
        relpath = path.relative_to(source).as_posix()
        if relpath.split("/")[0] in SKIPPED:
            continue
        yield path, relpath


def build(source: Path, output: Path) -> list[str]:
    written = []
    for path, relpath in pages(source):
        target = "index.html" if relpath == "README.md" else relpath[: -len(".md")] + ".html"
        destination = output / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render(path.read_text(encoding="utf-8"), "README.md" if relpath == "README.md" else relpath), encoding="utf-8")
        written.append(target)
    assets = source / "assets"
    if assets.is_dir():
        shutil.copytree(assets, output / "assets", dirs_exist_ok=True)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=Path("docs"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        print(f"refusing to build into a non-empty directory: {args.output}", file=sys.stderr)
        return 2
    written = build(args.source, args.output)
    print(f"{len(written)} pages written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
