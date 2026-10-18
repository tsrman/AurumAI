"""An established attachment journal cannot redirect source or tenant admission."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def load():
    path = Path(__file__).parents[1] / "prepare-data-lifecycle-attachment.py"
    spec = importlib.util.spec_from_file_location("lifecycle_attachment_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample():
    org, kb = str(uuid4()), str(uuid4())
    return {"org_id": org, "attachment": {
        "knowledge_base_id": kb, "skill_id": str(uuid4()), "operation_id": str(uuid4()),
        "file_metadata_id": str(uuid4()), "source_key": f"documents/{org}/{kb}/{uuid4()}.txt",
        "filename": "lifecycle-archive-attachment.txt",
    }}


def test_owned_attachment_source_is_accepted():
    load().validate_attachment(sample())


@pytest.mark.parametrize("kind", ["org", "kb", "source_name", "extension", "extra_segment"])
def test_source_journal_cannot_retarget_other_scope(kind):
    state = sample()
    source = state["attachment"]["source_key"].split("/")
    if kind == "org":
        source[1] = str(uuid4())
    elif kind == "kb":
        source[2] = str(uuid4())
    elif kind == "source_name":
        source[3] = "existing-document.txt"
    elif kind == "extension":
        source[3] = source[3].replace(".txt", ".pdf")
    else:
        source.append("another.txt")
    state["attachment"]["source_key"] = "/".join(source)
    with pytest.raises(ValueError):
        load().validate_attachment(state)


@pytest.mark.parametrize("kind", ["project", "service", "stopped", "binding"])
def test_minio_destination_is_owned_and_running(kind, monkeypatch):
    module = load()
    info = {"Config": {"Labels": {"com.docker.compose.project": module.PROJECT,
                                   "com.docker.compose.service": "minio"}},
            "State": {"Running": True}, "NetworkSettings": {"Ports": {
                "9000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "59000"}]}}}
    if kind in {"project", "service"}:
        info["Config"]["Labels"][f"com.docker.compose.{kind}"] = "foreign"
    elif kind == "stopped":
        info["State"]["Running"] = False
    else:
        info["NetworkSettings"]["Ports"]["9000/tcp"][0]["HostPort"] = "59001"
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps([info])))
    with pytest.raises(ValueError):
        module.owned("minio")


def test_embedded_metadata_loads_all_referenced_tables(monkeypatch):
    import os
    import subprocess
    import sys

    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    monkeypatch.setenv("APP_DEBUG", "false")

    module = load()
    code = module.BUSINESS.split('async def main():', 1)[0] + '''
from src.core.database import Base
try:
    list(Base.metadata.sorted_tables)
except Exception as error:
    raise SystemExit(type(error).__name__) from None
'''
    result = subprocess.run([sys.executable, '-c', code],
                            cwd=Path(__file__).parents[2] / 'business-backend',
                            env={
                                **os.environ,
                                'APP_DATABASE_URL': 'postgresql+asyncpg://fixture:fixture@localhost/fixture',
                                'APP_SECRET_KEY': 'metadata-fixture-' * 2,
                            },
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-200:]
