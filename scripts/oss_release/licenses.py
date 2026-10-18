"""Inventory upstream license evidence against exact dependency lock versions."""
from __future__ import annotations

import email.parser
import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .audit import Finding


@dataclass(frozen=True)
class DependencyRecord:
    ecosystem: str
    name: str
    version: str
    license: str
    lockfile: str
    evidence: str
    evidence_sha256: str


@dataclass(frozen=True)
class DependencyInventory:
    records: tuple[DependencyRecord, ...]
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict:
        return {"schema_version": 1, "dependencies": [asdict(r) for r in self.records],
                "findings": [asdict(f) for f in self.findings]}


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _notice_license(text: str) -> str:
    if "Permission is hereby granted, free of charge" in text:
        return "MIT"
    if "Apache License" in text and "Version 2.0" in text:
        return "Apache-2.0"
    if "GNU AFFERO GENERAL PUBLIC LICENSE" in text:
        return "AGPL-3.0 (see upstream notice)"
    if "Redistribution and use in source and binary forms" in text:
        return "BSD-3-Clause" if re.search(r"Neither the name|neither the name", text) else "BSD-2-Clause"
    return ""


def _metadata_license(metadata: dict) -> str:
    if metadata.get("license_expression"):
        return str(metadata["license_expression"])
    declared = str(metadata.get("license") or "").strip()
    if declared and declared.upper() not in {"UNKNOWN", "UNLICENSED", "NONE"}:
        return _notice_license(declared) or declared
    classifiers = metadata.get("classifiers", [])
    licenses = [c.split(" :: ")[-1] for c in classifiers if c.startswith("License :: ") and c != "License :: OSI Approved"]
    return " OR ".join(licenses) or "UNKNOWN"


def collect_dependency_inventory(source: Path, metadata_root: Path | None = None) -> DependencyInventory:
    metadata_root = metadata_root or source
    metadata: dict[tuple[str, str], tuple[dict, str, str]] = {}
    for path in metadata_root.glob("*/.venv/lib/python*/site-packages/*.dist-info/METADATA"):
        data = path.read_bytes()
        parsed = email.parser.BytesParser().parsebytes(data)
        info = {"license_expression": parsed.get("License-Expression"), "license": parsed.get("License"),
                "classifiers": parsed.get_all("Classifier", [])}
        key = (_normalized(parsed.get("Name", "")), parsed.get("Version", ""))
        metadata[key] = (info, "installed exact-version distribution METADATA", hashlib.sha256(data).hexdigest())
    evidence_path = source / "release/python-license-evidence.json"
    if evidence_path.is_file():
        for package in json.loads(evidence_path.read_text())["packages"]:
            key = (_normalized(package["name"]), package["version"])
            data = json.dumps(package, sort_keys=True).encode()
            metadata.setdefault(key, (package, package["source_url"], hashlib.sha256(data).hexdigest()))
    records: list[DependencyRecord] = []
    findings: list[Finding] = []
    for lock in sorted(source.glob("*/uv.lock")):
        name = lock.relative_to(source).as_posix()
        for package in tomllib.loads(lock.read_text()).get("package", []):
            if "registry" not in package.get("source", {}):
                continue
            info, evidence, digest = metadata.get((_normalized(package["name"]), package["version"]), ({}, "missing exact-version metadata", ""))
            license = _metadata_license(info)
            records.append(DependencyRecord("python", package["name"], package["version"], license, name, evidence, digest))
            if license == "UNKNOWN":
                findings.append(Finding(name, None, "dependency_license_unknown:" + package["name"]))
    locks = sorted(source.glob("*/package-lock.json"))
    if (source / "package-lock.json").exists():
        locks.append(source / "package-lock.json")
    for lock in locks:
        name = lock.relative_to(source).as_posix()
        for package_path, package in json.loads(lock.read_text()).get("packages", {}).items():
            if not package_path:
                continue
            package_name = package_path.rsplit("node_modules/", 1)[-1]
            version = package.get("version", "UNKNOWN")
            license = package.get("license") or "UNKNOWN"
            evidence = name
            digest = hashlib.sha256(json.dumps(package, sort_keys=True).encode()).hexdigest()
            distribution = metadata_root / lock.parent.relative_to(source) / package_path
            if license == "UNKNOWN" and (distribution / "package.json").is_file():
                installed = json.loads((distribution / "package.json").read_text())
                if installed.get("version") == version:
                    for notice in sorted(distribution.iterdir()):
                        if notice.is_file() and notice.name.casefold().startswith(("license", "copying")):
                            text = notice.read_text(errors="replace")
                            detected = _notice_license(text)
                            if detected:
                                license = detected
                                evidence = package_path + "/" + notice.name
                                digest = hashlib.sha256(notice.read_bytes()).hexdigest()
                                break
            records.append(DependencyRecord("npm", package_name, version, license, name, evidence, digest))
            if license == "UNKNOWN":
                findings.append(Finding(name, None, "dependency_license_unknown:" + package_name))
    return DependencyInventory(tuple(sorted(records, key=lambda r: (r.lockfile, r.name, r.version))), tuple(findings))
