"""Startup validation must reject stray dotenv fields without printing their values."""

import os
import subprocess
import sys
import shutil
from pathlib import Path


def test_startup_error_hides_unknown_dotenv_secret(tmp_path: Path) -> None:
    secret = "synthetic-provider-secret-do-not-print"
    config = tmp_path / "src/core/config.py"
    config.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).parents[1] / "src/core/config.py", config)
    (tmp_path / ".env").write_text(f"APP_DEBUG=true\nUNEXPECTED_PROVIDER_KEY={secret}\n")
    result = subprocess.run(
        [sys.executable, "-c", "import src.core.config"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "extra_forbidden" in result.stderr
    assert secret not in result.stderr + result.stdout
