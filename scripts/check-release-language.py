#!/usr/bin/env python3
"""Check English prose in selected public documentation and developer comments."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path

from oss_release.export import load_manifest, select_files
from oss_release.language import check_language


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    manifest = load_manifest(args.manifest or root / "release/oss-manifest.json")
    files = select_files(root, replace(manifest, required=()))
    findings = check_language(root, files)
    if args.json:
        print(json.dumps([asdict(f) for f in findings]))
    else:
        for finding in findings:
            print(f"{finding.path}:{finding.line or 0}: {finding.rule}")
        print(f"Language check: {len(files)} files, {len(findings)} findings.")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
