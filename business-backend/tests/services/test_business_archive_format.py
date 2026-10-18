from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
import importlib.util


def test_archive_has_explicit_typed_roundtrip(tmp_path):
    assert importlib.util.find_spec("src.services.archive") is not None, "archive package missing"
    from src.services.archive.schemas import encode_rows, read_rows, schema_for

    row = dict(
        id=uuid4(),
        org_id=uuid4(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        user_id=uuid4(),
        title=None,
        model="test",
        mode=None,
        system_prompt=None,
        status="deleted",
        temperature=None,
        extra={"nested": None},
        token_count_in=2,
        token_count_out=3,
        cost_usd=Decimal("0.123456"),
        last_message_at=None,
    )
    path = tmp_path / "part.parquet"
    encode_rows("conversations", [row], path)
    assert list(read_rows("conversations", path)) == [row]
    assert schema_for("conversations").field("cost_usd").type.scale == 6
    assert path.stat().st_mode & 0o777 == 0o600


def test_archive_rejects_oversize_cell(tmp_path):
    assert importlib.util.find_spec("src.services.archive") is not None, "archive package missing"
    from src.services.archive.schemas import encode_rows, ArchiveFormatError
    import pytest

    with pytest.raises(ArchiveFormatError, match="cell_too_large"):
        encode_rows(
            "conversations", [{"title": "x" * (1024 * 1024 + 1)}], tmp_path / "part.parquet"
        )


def test_clean_operator_process_resolves_every_archived_foreign_key():
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
try:
    import src.cli.archive_data
    from src.services.archive.snapshot import TABLES
    resolved = 0
    for table in TABLES.values():
        for constraint in table.foreign_key_constraints:
            for element in constraint.elements:
                column = element.column
                resolved += 1
    print(json.dumps({'resolved': resolved}))
except Exception as error:
    print(json.dumps({'error_type': type(error).__name__}))
    raise SystemExit(1) from None
""",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=20,
    )
    # Child emits only schema-count or safe exception type; no settings/payload.
    assert result.returncode == 0, result.stdout
    import json

    assert json.loads(result.stdout)["resolved"] > 0
