from __future__ import annotations

from pathlib import Path
from runpy import run_path

import pytest


ROOT = Path(__file__).resolve().parents[2]

ACTIVE_ROOTS = (
    ROOT / "business-backend" / "src",
    ROOT / "business-backend" / "tests",
    ROOT / "business-ui" / "src",
    ROOT / "business-ui" / "tests",
    ROOT / "business-ui" / "e2e",
    ROOT / "admin-ui" / "src",
    ROOT / "admin-ui" / "tests",
    ROOT / "admin-ui" / "e2e",
    ROOT / "charts" / "aurum-ai",
    ROOT / "scripts",
)

ACTIVE_FILES = (
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "ROADMAP.md",
    ROOT / "docker-compose.yml",
    ROOT / "smoke-test-groups.sh",
    ROOT / "smoke-test-k8s.sh",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "CONFIGURATION.md",
)

FORBIDDEN_MARKERS = (
    "AUTHORIZATION_V2_",
    "authorizationV2",
    "model:<",
    "permission_catalog",
    "GroupPermission",
)

FORBIDDEN_PATHS = (
    ROOT / "business-backend" / "src" / "core" / "permission_catalog.py",
    ROOT / "business-backend" / "src" / "core" / "permissions.py",
    ROOT / "business-backend" / "src" / "models" / "group_permission.py",
    ROOT / "business-backend" / "src" / "models" / "authorization_migration_run.py",
    ROOT / "business-backend" / "src" / "services" / "authorization_migration.py",
    ROOT / "business-backend" / "src" / "cli" / "backfill_authorization_v2.py",
    ROOT / "business-ui" / "src" / "lib" / "permissions.ts",
    ROOT / "admin-ui" / "src" / "lib" / "permissions.ts",
)

REMOVAL_MIGRATION = (
    ROOT
    / "business-backend"
    / "alembic"
    / "versions"
    / "2026_09_01_remove_legacy_authorization.py"
)


def _active_text_files() -> list[Path]:
    paths: set[Path] = set(ACTIVE_FILES)
    for root in ACTIVE_ROOTS:
        if not root.exists():
            continue
        paths.update(
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in {".md", ".py", ".sh", ".ts", ".tsx", ".yaml", ".yml"}
            and path != Path(__file__).resolve()
            and "node_modules" not in path.parts
            and ".next" not in path.parts
            and ".venv" not in path.parts
        )
    return sorted(paths)


@pytest.mark.parametrize("path", FORBIDDEN_PATHS, ids=lambda path: path.name)
def test_legacy_authorization_modules_are_removed(path: Path) -> None:
    """Reintroducing a legacy module would restore a second authorization source."""
    assert not path.exists(), path.relative_to(ROOT)


def test_active_repository_has_no_legacy_authorization_markers() -> None:
    """Active runtime/config/UI must not regain flags, shadow or permission conventions."""
    findings: list[str] = []
    for path in _active_text_files():
        text = path.read_text()
        for line_number, line in enumerate(text.splitlines(), start=1):
            for marker in FORBIDDEN_MARKERS:
                if marker in line:
                    findings.append(
                        f"{path.relative_to(ROOT)}:{line_number}: {marker}"
                    )
    assert findings == []


def test_session_auth_has_no_embedded_permission_compatibility() -> None:
    """A signed legacy permission snapshot must not remain an accepted session format."""
    auth_source = (ROOT / "business-backend" / "src" / "core" / "auth.py").read_text()
    token_source = (
        ROOT / "business-backend" / "src" / "services" / "session_token.py"
    ).read_text()
    assert 'payload.get("permissions"' not in auth_source
    assert 'payload["permissions"]' not in token_source


def test_head_migration_drops_only_the_legacy_authorization_schema() -> None:
    """Existing development databases must converge through an explicit, reviewable migration."""
    source = REMOVAL_MIGRATION.read_text()
    migration = run_path(str(REMOVAL_MIGRATION))
    assert migration["down_revision"] == "2026_09_01_agent_definitions"
    assert 'op.drop_table("group_permissions")' in source
    assert 'op.drop_column("skills", "permissions")' in source
    assert 'op.drop_table("authorization_migration_runs")' in source
