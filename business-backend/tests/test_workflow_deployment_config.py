from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]


def test_compose_exposes_identical_workflow_limits_to_both_entrypoints() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    expected = {
        "APP_WORKFLOW_MAX_NODES": "${APP_WORKFLOW_MAX_NODES:-20}",
        "APP_WORKFLOW_MAX_PARALLELISM": "${APP_WORKFLOW_MAX_PARALLELISM:-4}",
        "APP_WORKFLOW_DIAGNOSTIC_RETENTION_DAYS": (
            "${APP_WORKFLOW_DIAGNOSTIC_RETENTION_DAYS:-30}"
        ),
        "APP_WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS": (
            "${APP_WORKFLOW_RETENTION_SWEEP_INTERVAL_SECONDS:-3600}"
        ),
    }
    for service_name in ("backend", "backend-admin"):
        environment = compose["services"][service_name]["environment"]
        assert {name: environment.get(name) for name in expected} == expected


def test_retention_reaper_is_owned_only_by_business_entrypoint() -> None:
    business = (ROOT / "business-backend/src/main_business.py").read_text()
    admin = (ROOT / "business-backend/src/main_admin.py").read_text()

    assert business.count("workflow_run_service.run_retention_reaper()") == 1
    assert "workflow_run_service" not in admin
    assert "run_retention_reaper" not in admin
    assert business.count("run_interaction_outbox_dispatcher()") == 1
    assert "run_interaction_outbox_dispatcher" not in admin


def test_compose_exposes_question_wait_maintenance_limits() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    environment = compose["services"]["backend"]["environment"]

    assert environment.get("APP_INTERACTION_MAINTENANCE_INTERVAL_SECONDS") == (
        "${APP_INTERACTION_MAINTENANCE_INTERVAL_SECONDS:-30}"
    )
    assert environment.get("APP_INTERACTION_SETTLEMENT_GRACE_SECONDS") == (
        "${APP_INTERACTION_SETTLEMENT_GRACE_SECONDS:-300}"
    )
    assert environment.get("APP_RUNNER_TOKEN_REFRESH_AFTER_SECONDS") == (
        "${APP_RUNNER_TOKEN_REFRESH_AFTER_SECONDS:-1200}"
    )
