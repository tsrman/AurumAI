#!/usr/bin/env python3
"""Export an explicitly selected AurumAI release tree; never copy local secrets."""
from __future__ import annotations

import argparse
import json
import os
import hashlib
import sys
import re
import stat
from dataclasses import asdict
from pathlib import Path

from oss_release.export import export_candidate, load_manifest, _safe_path
from oss_release.audit import Finding, audit_tree
from oss_release.language import check_language
from oss_release.docs import check_links


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source", type=Path, required=True)
    export.add_argument("--destination", type=Path, required=True)
    export.add_argument("--manifest", type=Path, default=Path("release/oss-manifest.json"))
    export.add_argument("--report", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--candidate", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--inventory", type=Path, required=True)
    verify.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.report.exists():
            raise FileExistsError("report_exists")
        roots = [args.source, args.destination] if args.command == "export" else [args.candidate]
        report_path = args.report.resolve()
        for root in roots:
            if root.is_symlink():
                raise ValueError("unsafe_root")
            resolved = root.resolve()
            if report_path == resolved or resolved in report_path.parents:
                raise ValueError("report_inside_release_tree")
        manifest = load_manifest(args.manifest)
        if args.command == "export":
            inventory = export_candidate(args.source, args.destination, manifest)
            report = {"schema_version": 1, "files": [asdict(r) for r in inventory.records]}
            status = 0
        else:
            previous = json.loads(args.inventory.read_text())
            if not isinstance(previous, dict) or set(previous) != {"schema_version", "files"} or previous["schema_version"] != 1:
                raise ValueError("invalid_inventory")
            records = previous["files"]
            if not isinstance(records, list):
                raise ValueError("invalid_inventory")
            expected_paths = set()
            for item in records:
                if not isinstance(item, dict) or set(item) != {"path", "sha256", "size", "mode"}:
                    raise ValueError("invalid_inventory")
                name = item["path"]
                if not isinstance(name, str) or not _safe_path(name) or name in expected_paths:
                    raise ValueError("invalid_inventory")
                if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                    raise ValueError("invalid_inventory")
                if type(item["size"]) is not int or item["size"] < 0 or item["size"] > 64 * 1024 * 1024:
                    raise ValueError("invalid_inventory")
                if type(item["mode"]) is not int or item["mode"] not in {0o644, 0o755}:
                    raise ValueError("invalid_inventory")
                expected_paths.add(name)
            findings = list(audit_tree(args.candidate, manifest))
            actual_paths = {
                p.relative_to(args.candidate).as_posix()
                for p in args.candidate.rglob("*") if p.is_file() or p.is_symlink()
            }
            if actual_paths != expected_paths:
                findings.append(Finding(".", None, "inventory_file_set_changed"))
            # Unsafe files must not be reopened by downstream document parsers.
            if not findings:
                for item in records:
                    name = item["path"]
                    path = args.candidate / name
                    info = path.stat()
                    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                        findings.append(Finding(name, None, "inventory_content_changed"))
                    if info.st_size != item["size"]:
                        findings.append(Finding(name, None, "inventory_size_changed"))
                    if stat.S_IMODE(info.st_mode) != item["mode"]:
                        findings.append(Finding(name, None, "inventory_mode_changed"))
                findings.extend(check_language(args.candidate, tuple(sorted(actual_paths))))
                findings.extend(check_links(args.candidate, tuple(sorted(actual_paths))))
            report = {"schema_version": 1, "findings": [asdict(f) for f in findings]}
            status = int(bool(findings))
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print(f"Release {args.command}: {'PASS' if status == 0 else 'BLOCKED'}; private report written.")
        return status
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Release preparation failed ({type(exc).__name__}); raw diagnostics suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
