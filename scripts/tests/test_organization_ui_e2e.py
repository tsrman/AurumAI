from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
WRAPPER = ROOT / "smoke-test-organization-ui.sh"
QUOTA_SMOKE = ROOT / "smoke-test-quota.sh"
BUSINESS_APP = ROOT / "scripts" / "quota-test-app.py"
ADMIN_APP = ROOT / "scripts" / "quota-test-admin-app.py"
SPEC = ROOT / "business-ui" / "e2e" / "organization.real.spec.ts"


def test_wrapper_uses_fixed_registered_origins_and_opt_in_quota_mode() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert "E2E_ORGANIZATION_UI=true" in source
    assert "QUOTA_E2E_BACKEND_PORT=8000" in source
    assert "QUOTA_E2E_ADMIN_PORT=8001" in source
    assert "http://localhost:3000" in source
    assert "http://localhost:3100" in source


def test_wrapper_checks_exact_compose_ownership_before_replacement() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert "com.docker.compose.project" in source
    assert "com.docker.compose.service" in source
    assert 'require_owned_service "aurum-ai-backend" "backend"' in source
    assert (
        'require_owned_service "aurum-ai-backend-admin" "backend-admin"'
        in source
    )
    assert '-p "$COMPOSE_PROJECT_NAME"' in source
    assert "stop backend backend-admin" in source


def test_wrapper_restores_both_backends_from_exit_trap() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert "trap cleanup EXIT INT TERM" in source
    assert "up -d --no-deps --force-recreate backend backend-admin" in source
    assert 'wait_for_url "http://localhost:8000/ready" "backend"' in source
    assert 'wait_for_url "http://localhost:8001/ready" "backend-admin"' in source


def test_wrapper_reuses_the_owned_stack_secret_without_printing_it() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    assert 'STACK_APP_SECRET_KEY=""' in source
    assert "s/^APP_SECRET_KEY=//p" in source
    assert 'APP_SECRET_KEY="$STACK_APP_SECRET_KEY" docker compose' in source
    assert "STACK_APP_SECRET_KEY" not in source.replace(
        'APP_SECRET_KEY="$STACK_APP_SECRET_KEY"', ""
    ).replace('STACK_APP_SECRET_KEY=""', "").replace(
        'STACK_APP_SECRET_KEY=$(docker inspect -f', ""
    ).replace('[[ -n "$STACK_APP_SECRET_KEY" ]]', "")
    compose_commands = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(
            ("docker compose", 'APP_SECRET_KEY="$STACK_APP_SECRET_KEY" docker compose')
        )
    ]
    assert compose_commands
    assert all(
        line.startswith('APP_SECRET_KEY="$STACK_APP_SECRET_KEY" docker compose')
        for line in compose_commands
    )


def test_wrapper_has_valid_shell_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(WRAPPER)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_business_test_app_mounts_models_and_exact_business_cors() -> None:
    source = BUSINESS_APP.read_text(encoding="utf-8")
    assert "from fastapi.middleware.cors import CORSMiddleware" in source
    assert "from src.routers import auth, chat, health, models, settings, usage" in source
    assert 'app.include_router(models.router, prefix="/v1")' in source
    assert 'allow_origins=["http://localhost:3000"]' in source


def test_admin_test_app_mounts_authorization_and_exact_admin_cors() -> None:
    source = ADMIN_APP.read_text(encoding="utf-8")
    assert "from fastapi.middleware.cors import CORSMiddleware" in source
    assert "authorization_admin," in source
    assert (
        'app.include_router(authorization_admin.router, '
        'prefix="/v1/admin/authorization")'
    ) in source
    assert 'allow_origins=["http://localhost:3100"]' in source


def test_quota_browser_phase_is_explicit_and_single_worker() -> None:
    source = QUOTA_SMOKE.read_text(encoding="utf-8")
    assert 'if [[ "${E2E_ORGANIZATION_UI:-false}" != "true" ]]; then' in source
    assert 'E2E_ORGANIZATION_UI_MODEL="$MODEL_ID"' in source
    assert 'E2E_ORGANIZATION_UI_USER_ID="$user_id"' in source
    assert 'E2E_ORGANIZATION_UI_ADMIN_API_URL="$admin_url"' in source
    assert 'E2E_ORGANIZATION_UI_PERIOD_FROM="$period_from"' in source
    assert 'E2E_ORGANIZATION_UI_PERIOD_TO="$period_to"' in source
    assert "e2e/organization.real.spec.ts" in source
    assert "--project=chromium --workers=1 --trace=off" in source


def test_normal_quota_smoke_does_not_enable_browser_mode() -> None:
    source = QUOTA_SMOKE.read_text(encoding="utf-8")
    assert 'E2E_ORGANIZATION_UI="${E2E_ORGANIZATION_UI:-false}"' in source
    assert (
        'run_browser_acceptance "$user_id" "$period_from" "$period_to"'
        in source
    )


def test_browser_spec_covers_cross_plane_lifecycle_without_trace() -> None:
    source = SPEC.read_text(encoding="utf-8")
    required = [
        "test.skip(!ENABLED",
        'trace: "off"',
        '"admin-ui"',
        '"business-ui"',
        '"/admin/models"',
        '"/admin/analytics"',
        '"/v1/admin/authorization/grants/reconcile"',
        '"quota-e2e-model"',
        '"Requests remaining: 7"',
        '"Tokens remaining: 76"',
        '"1 request without token data"',
        '"The saved model is no longer available to the organization."',
    ]
    for fragment in required:
        assert fragment in source
    assert "console.log(token" not in source
    assert "eyJ" not in source


def test_browser_spec_asserts_native_select_options_without_visibility() -> None:
    source = SPEC.read_text(encoding="utf-8")
    assert "locator('option[value=" in source
    assert 'getByRole("option"' not in source


def test_browser_spec_reads_each_business_state_in_a_fresh_authenticated_context() -> None:
    source = SPEC.read_text(encoding="utf-8")
    assert "async function openBusinessSettings" in source
    assert source.count("await openBusinessSettings(browser)") == 3
    assert source.index("await openBusinessSettings(browser)") > source.index(
        "await saved;"
    )
    assert 'businessPage.goto(`${BUSINESS_URL}/settings`)' not in source
    assert "businessPage.reload()" not in source
