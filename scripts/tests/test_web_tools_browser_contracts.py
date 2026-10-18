"""Real local-browser artifact canary; opt-in where Chromium is installed."""
import os
from pathlib import Path
import subprocess
import uuid

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.skipif(os.getenv("RUN_WEB_BROWSER_CONTRACTS") != "1", reason="requires installed Chromium; run manually with RUN_WEB_BROWSER_CONTRACTS=1")
@pytest.mark.parametrize(
    ("phase", "safe_phase"),
    [
        ("reconnect", "reconnect"),
        ("sources-api", "sources-api"),
        ("sources-turn-render", "sources-turn-render"),
        ("history-reload-auth", "history-reload-auth"),
        ("history-navigation-auth", "history-navigation-auth"),
        ("forbidden-content", "unknown"),
    ],
)
def test_failed_browser_run_does_not_persist_page_content_or_diagnostics(
    tmp_path, phase, safe_phase
):
    canary = "private-page-canary-" + uuid.uuid4().hex
    reached = tmp_path / "reached"
    output = tmp_path / "artifacts"
    result = subprocess.run(["npm", "exec", "--", "playwright", "test", "e2e/web-tools.privacy-canary.spec.ts",
        "--config=playwright.web-tools.config.ts", "--workers=1"], cwd=ROOT / "business-ui",
        env={**os.environ, "WEB_PRIVACY_CANARY": canary, "WEB_PRIVACY_REACHED": str(reached),
             "WEB_PRIVACY_PHASE": phase if safe_phase != "unknown" else canary,
             "E2E_WEB_OUTPUT_DIR": str(output)}, capture_output=True, text=True, timeout=60)
    assert reached.exists(), "the canary must reach a real browser page before its intentional failure"
    assert result.returncode != 0
    assert canary not in result.stdout + result.stderr
    assert f"[web-phase:{safe_phase}]" in result.stdout
    for artifact in output.rglob("*"):
        if artifact.is_file():
            assert artifact.suffix not in {".zip", ".png", ".webm", ".har"}
            assert canary.encode() not in artifact.read_bytes(), "private page content reached a failure artifact"
