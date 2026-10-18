from pathlib import Path
from typing import Any

import pytest
import yaml

from src.core.config import Settings


OBSOLETE_PREFIX = "APP_AUTHORIZATION" + "_V2_"


def _compose_services() -> dict[str, Any]:
    path = Path(__file__).parents[2] / "docker-compose.yml"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded["services"]


def test_compose_has_no_authorization_transition_environment() -> None:
    services = _compose_services()

    for service_name in ("backend", "backend-admin"):
        environment = services[service_name]["environment"]
        assert not any(name.startswith(OBSOLETE_PREFIX) for name in environment)


def test_web_access_operator_switch_defaults_off() -> None:
    settings = Settings(_env_file=None, DEBUG=True)

    assert settings.WEB_ENABLED is False


def test_enabled_web_access_requires_a_dedicated_service_secret() -> None:
    with pytest.raises(ValueError, match="WEB_TOOLS_SERVICE_SECRET"):
        Settings(_env_file=None, DEBUG=True, WEB_ENABLED=True)

    configured = Settings(
        _env_file=None,
        DEBUG=True,
        WEB_ENABLED=True,
        WEB_TOOLS_SERVICE_SECRET="x" * 32,
    )
    assert configured.WEB_TOOLS_SERVICE_SECRET.get_secret_value() == "x" * 32


@pytest.mark.parametrize(
    "url",
    [
        "http://web-tools",
        "ftp://web-tools:8120",
        "http://user:pass@web-tools:8120",
        "http://web-tools:8120/internal",
        "http://web-tools:8120?redirect=outside",
        "http://web-tools:8120#outside",
    ],
)
def test_enabled_web_access_requires_an_exact_internal_service_origin(url: str) -> None:
    with pytest.raises(ValueError, match="WEB_TOOLS_URL"):
        Settings(
            _env_file=None,
            DEBUG=True,
            WEB_ENABLED=True,
            WEB_TOOLS_URL=url,
            WEB_TOOLS_SERVICE_SECRET="x" * 32,
        )
