"""Integer settings must reach the pods as integers, never as Helm's float spelling."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
CHART = ROOT / "charts" / "aurum-ai"
# Helm reads a large integer value as a float and prints it as 1.048576e+07.
FLOAT_SPELLING = re.compile(r"^-?\d+(\.\d+)?e[+-]?\d+$", re.IGNORECASE)


def _render(*values: str) -> list[dict]:
    command = ["helm", "template", "env-test", str(CHART), "--namespace", "platform"]
    for value in values:
        command.extend(("--set", value))
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _env_values(documents: list[dict]) -> list[tuple[str, str, str]]:
    found = []
    for document in documents:
        if document.get("kind") not in {"Deployment", "Job"}:
            continue
        spec = document["spec"]["template"]["spec"] if document["kind"] == "Deployment" else document["spec"]["template"]["spec"]
        for container in spec.get("containers", []):
            for item in container.get("env", []):
                if "value" in item:
                    found.append((document["metadata"]["name"], item["name"], str(item["value"])))
    return found


@pytest.mark.parametrize(
    "values",
    [
        (),
        ("hermes.enabled=true", "hermes.namespace=hermes-agents"),
        (
            "hermes.enabled=true",
            "hermes.namespace=hermes-agents",
            "hermes.preview.enabled=true",
            "hermes.preview.origin=https://{preview}.preview.example.org",
            "hermes.preview.host=*.preview.example.org",
        ),
    ],
)
def test_no_container_environment_value_uses_a_float_spelling(values: tuple[str, ...]) -> None:
    offenders = [
        (workload, name, value)
        for workload, name, value in _env_values(_render(*values))
        if FLOAT_SPELLING.match(value)
    ]
    assert offenders == [], f"large integers spelled as floats: {offenders}"


def test_the_image_upload_limit_is_an_integer() -> None:
    values = {name: value for _, name, value in _env_values(_render())}
    assert values["APP_IMAGES_UPLOAD_MAX_BYTES"] == "10485760"
