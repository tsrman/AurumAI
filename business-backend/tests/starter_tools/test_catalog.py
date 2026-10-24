"""Executable contracts for the offline starter catalog."""

import hashlib
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


def catalog(endpoint="http://mcp-everything:3001/sse"):
    return importlib.import_module("src.services.starter_tools").starter_catalog(endpoint)


def execute_bundle(tmp_path: Path, key: str, filename: str, data: str):
    entry = next(item for item in catalog() if item.key == key)
    with zipfile.ZipFile(io.BytesIO(entry.bundle)) as bundle:
        bundle.extractall(tmp_path)
    source = tmp_path / filename
    source.write_text(data)
    script = "analyze.py" if key == "csv-statistics" else "format_json.py"
    result = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / script), str(source)],
        capture_output=True, text=True, timeout=10,
    )
    return result


def test_catalog_is_deterministic_and_uses_sse_without_credentials():
    first, second = catalog(), catalog()
    assert len(first) == 4
    assert [entry.id for entry in first] == [entry.id for entry in second]
    assert len({entry.id for entry in first}) == 4
    for left, right in zip(first, second, strict=True):
        assert left.bundle == right.bundle
        if left.bundle:
            assert left.checksum == hashlib.sha256(left.bundle).hexdigest()
        else:
            assert left.values["mcp_transport"] == "sse"
            assert left.values["mcp_endpoint"] == "http://mcp-everything:3001/sse"
            assert left.values.get("mcp_auth_token") is None
    assert {entry.values["activity_name"] for entry in first if not entry.bundle} == {
        "get-sum", "echo",
    }


@pytest.mark.parametrize("endpoint", [
    "file:///tmp/server", "http://user:secret@example.com/sse",
    "https://example.com/sse?token=secret", "http://example.com/sse#secret",
])
def test_endpoint_cannot_embed_credentials(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        catalog(endpoint)


def test_csv_statistics_computes_real_totals_and_missing_values(tmp_path):
    result = execute_bundle(tmp_path, "csv-statistics", "input.csv", "team,sales\nA,10\nB,20\nC,\n")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["rows"] == 3
    assert output["numeric"]["sales"] == {
        "count": 2, "missing": 1, "sum": 30, "mean": 15, "min": 10, "max": 20,
    }
    assert "team" not in output["numeric"]


@pytest.mark.parametrize("data", ["x,x\n1,2\n", "x,y\n1\n", "", "x\nNaN\n"])
def test_csv_rejects_ambiguous_or_nonfinite_data(tmp_path, data):
    result = execute_bundle(tmp_path, "csv-statistics", "input.csv", data)
    assert result.returncode != 0
    assert "Error:" in result.stderr


def test_json_formats_valid_document_without_changing_values(tmp_path):
    result = execute_bundle(tmp_path, "json-format", "input.json", '{"z":[1,true,null],"a":"text"}')
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{\n  "a": "text",\n  "z": [\n    1,\n    true,\n    null\n  ]\n}\n'


@pytest.mark.parametrize("data", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', '{oops'])
def test_json_rejects_invalid_duplicate_and_nonfinite_values(tmp_path, data):
    result = execute_bundle(tmp_path, "json-format", "input.json", data)
    assert result.returncode != 0
    assert "Error:" in result.stderr


def test_cli_dry_run_needs_neither_database_nor_storage(monkeypatch, capsys):
    cli = importlib.import_module("src.cli.install_starter_tools")
    def unexpected():
        raise AssertionError("dry run must not open a database")
    monkeypatch.setattr(cli, "_runtime_session_factory", unexpected)
    assert cli.main([]) == 0
    output = capsys.readouterr().out
    assert "dry run" in output.lower()
    assert "csv-statistics" in output


@pytest.mark.parametrize("key,filename,data", [
    ("csv-statistics", "input.csv", "x\n" + "1\n" * 10001),
    ("csv-statistics", "input.csv", "x\n" + "1" * (1024 * 1024)),
    ("json-format", "input.json", '"' + "x" * (1024 * 1024) + '"'),
    ("json-format", "input.json", "[" * 2000 + "]" * 2000),
], ids=["row-limit", "csv-byte-limit", "json-byte-limit", "json-depth-limit"])
def test_bundle_input_limits_fail_cleanly(tmp_path, key, filename, data):
    result = execute_bundle(tmp_path, key, filename, data)
    assert result.returncode != 0
    assert "Error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_json_formatting_preserves_decimal_precision(tmp_path):
    source = '{"amount":0.12345678901234567890123456789,"small":1.00000000000000000001e-20}'
    result = execute_bundle(tmp_path, "json-format", "input.json", source)
    assert result.returncode == 0, result.stderr
    assert "0.12345678901234567890123456789" in result.stdout
    assert "1.00000000000000000001e-20" in result.stdout
