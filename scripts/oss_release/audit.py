"""Bounded source/ZIP inspection with metadata-only findings."""
from __future__ import annotations

import io
import base64
import binascii
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .export import ReleaseManifest, _matches, _safe_path, prohibited_path

MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 500
MAX_FILE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    rule: str


RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "aws_access_id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "provider_token": re.compile(r"\b(?:sk-proj-|sk-ant-|sk-or-v1-)[A-Za-z0-9_-]{20,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b"),
    "cloud_account": re.compile(r"\b(?:b1g|b1i|aje|f8d)[a-z0-9]{16,20}\b"),
    "personal_path": re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
}


def scan_text(data: bytes, name: str) -> list[Finding]:
    text = data.decode("utf-8", errors="replace")
    findings = [
        Finding(name, line_number, rule)
        for line_number, line in enumerate(text.splitlines(), 1)
        for rule, pattern in RULES.items() if pattern.search(line)
    ]
    # Temporal histories store payload bytes as base64, including plain JSON.
    # Inspect those bytes without deserializing application objects or extracting files.
    if name.endswith(".json"):
        try:
            pending = [(json.loads(text), 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 100:
                    findings.append(Finding(name, None, "json_limit"))
                    break
                if isinstance(value, dict):
                    if isinstance(value.get("metadata"), dict) and isinstance(value.get("data"), str):
                        try:
                            payload = base64.b64decode(value["data"], validate=True)
                        except (ValueError, binascii.Error):
                            findings.append(Finding(name, None, "invalid_payload_encoding"))
                        else:
                            findings.extend(scan_text(payload, name + "!payload"))
                    pending.extend((item, depth + 1) for item in value.values())
                elif isinstance(value, list):
                    pending.extend((item, depth + 1) for item in value)
        except (ValueError, RecursionError):
            findings.append(Finding(name, None, "invalid_json"))
    return findings


def _scan_archive(data: bytes, name: str, depth: int = 0) -> list[Finding]:
    if depth > 1:
        return [Finding(name, None, "archive_limit")]
    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES:
                return [Finding(name, None, "archive_limit")]
            seen: set[str] = set()
            for member in members:
                member_name = member.filename.rstrip("/")
                nested = name + "!" + member_name
                if not _safe_path(member_name) or member_name in seen:
                    findings.append(Finding(name, None, "unsafe_archive_path"))
                    continue
                seen.add(member_name)
                if stat.S_ISLNK(member.external_attr >> 16):
                    findings.append(Finding(nested, None, "unsafe_archive_path"))
                    continue
                if member.is_dir():
                    continue
                if prohibited_path(member_name):
                    findings.append(Finding(nested, None, "prohibited_path"))
                    continue
                with archive.open(member) as stream:
                    payload = stream.read(MAX_ARCHIVE_BYTES + 1)
                if len(payload) > MAX_ARCHIVE_BYTES:
                    findings.append(Finding(nested, None, "archive_limit"))
                elif member_name.endswith(".zip"):
                    findings.extend(_scan_archive(payload, nested, depth + 1))
                else:
                    findings.extend(scan_text(payload, nested))
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
        findings.append(Finding(name, None, "invalid_archive"))
    return findings


def audit_tree(root: Path, manifest: ReleaseManifest) -> tuple[Finding, ...]:
    root = root.resolve(strict=True)
    findings: list[Finding] = []
    present: set[str] = set()
    for directory, folders, files in os.walk(root, followlinks=False):
        for folder in list(folders):
            path = Path(directory) / folder
            name = path.relative_to(root).as_posix()
            if path.is_symlink() or prohibited_path(name):
                findings.append(Finding(name, None, "unsafe_source_file" if path.is_symlink() else "prohibited_path"))
                folders.remove(folder)
        for filename in sorted(files):
            path = Path(directory) / filename
            name = path.relative_to(root).as_posix()
            present.add(name)
            if path.is_symlink() or not path.is_file():
                findings.append(Finding(name, None, "unsafe_source_file"))
            elif prohibited_path(name):
                findings.append(Finding(name, None, "prohibited_path"))
            elif not _matches(name, manifest.include) or _matches(name, manifest.exclude):
                findings.append(Finding(name, None, "unselected_file"))
            elif path.stat().st_size > MAX_FILE_BYTES:
                findings.append(Finding(name, None, "file_limit"))
            else:
                data = path.read_bytes()
                findings.extend(_scan_archive(data, name) if name.endswith(".zip") else scan_text(data, name))
    for missing in sorted(set(manifest.required) - present):
        findings.append(Finding(missing, None, "missing_required_file"))
    return tuple(sorted(findings, key=lambda f: (f.path, f.line or 0, f.rule)))
