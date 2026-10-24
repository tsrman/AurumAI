"""Additive operator-owned starter catalog and durable tool-only grant receipts."""

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5
import zipfile

from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.access_grant import AccessGrant
from src.models.authorization_audit_event import AuthorizationAuditEvent
from src.models.organization import Organization
from src.models.skill import Skill
from src.services.object_storage import object_storage
from src.services.skill_bundle import validate_and_parse
from src.services.skill_grant_provisioning import skill_grant_provisioning_service

DEFAULT_MCP_ENDPOINT = "http://mcp-everything:3001/sse"
STARTER_NAMESPACE = UUID("37b92ba0-27e4-55be-b645-7361fbca4c4b")
OWNERSHIP_PREFIX = "aurum-starter-v1:"
_BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "starter_bundles"


@dataclass(frozen=True)
class StarterTool:
    key: str
    id: UUID
    values: dict[str, Any]
    bundle: bytes | None = None
    checksum: str | None = None


class BundleStorage(Protocol):
    async def put_bundle(self, key: str, data: bytes) -> None: ...


class StarterBundleStorage:
    async def put_bundle(self, key: str, data: bytes) -> None:
        """Never replace an object, even after an uncertain upload response."""
        async with object_storage._client() as client:
            try:
                await client.put_object(
                    Bucket=settings.S3_BUCKET,
                    Key=key, Body=data, IfNoneMatch="*",
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                    raise
                if await object_storage.get_bytes(key) != data:
                    raise ValueError("starter bundle identity collision") from None


def _bundle(directory: str) -> bytes:
    root = _BUNDLE_ROOT / directory
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative in ("SKILL.md", *sorted(
            str(path.relative_to(root)) for path in (root / "scripts").glob("*.py")
        )):
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (root / relative).read_bytes())
    return buffer.getvalue()


def starter_catalog(mcp_endpoint: str = DEFAULT_MCP_ENDPOINT) -> tuple[StarterTool, ...]:
    try:
        endpoint = urlsplit(mcp_endpoint)
        port = endpoint.port
    except ValueError:
        raise ValueError("MCP endpoint must be an HTTP(S) URL without credentials") from None
    if (
        endpoint.scheme not in {"http", "https"} or not endpoint.hostname
        or endpoint.username is not None or endpoint.password is not None
        or endpoint.query or endpoint.fragment or port == 0
    ):
        raise ValueError("MCP endpoint must be an HTTP(S) URL without credentials")
    entries = []
    for key, directory, name in (
        ("csv-statistics", "csv_statistics", "CSV statistics"),
        ("json-format", "json_format", "JSON validation and formatting"),
    ):
        bundle = _bundle(directory)
        parsed = validate_and_parse(bundle)
        skill_id = uuid5(STARTER_NAMESPACE, key)
        entries.append(StarterTool(key, skill_id, {
            "id": skill_id, "org_id": None, "name": name, "type": "code",
            "description": f"{parsed.skill_description}\n\n{parsed.skill_md_body}",
            "workflow_type": OWNERSHIP_PREFIX + key, "network_policy": "none",
            "bundle_storage_key": f"starter-tools/v1/{skill_id}/{parsed.checksum}.zip",
            "bundle_checksum": parsed.checksum, "bundle_size_bytes": parsed.size_bytes,
            "bundle_skill_name": parsed.skill_name, "input_schema": {},
        }, bundle, hashlib.sha256(bundle).hexdigest()))
    for key, name, activity, description, properties in (
        ("calculator", "Calculator (MCP)", "get-sum", "Add two numbers through the local MCP server.",
         {"a": {"type": "number"}, "b": {"type": "number"}}),
        ("text-round-trip", "Text round-trip (MCP)", "echo", "Echo supplied text through the local MCP server.",
         {"message": {"type": "string"}}),
    ):
        skill_id = uuid5(STARTER_NAMESPACE, key)
        entries.append(StarterTool(key, skill_id, {
            "id": skill_id, "org_id": None, "name": name, "type": "mcp",
            "description": description, "workflow_type": OWNERSHIP_PREFIX + key,
            "activity_name": activity, "mcp_transport": "sse", "mcp_endpoint": mcp_endpoint,
            "mcp_auth_token": None, "network_policy": "none",
            "input_schema": {"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False},
        }))
    return tuple(entries)


def _assert_owned(skill: Skill, tool: StarterTool) -> None:
    if (skill.org_id is not None or skill.owning_conversation_id is not None
            or skill.workflow_type != OWNERSHIP_PREFIX + tool.key):
        raise ValueError(f"starter tool identity collision: {tool.id}")


async def install_starter_tools(
    session: AsyncSession,
    storage: BundleStorage,
    *,
    organization_ids: Sequence[UUID] = (),
    mcp_endpoint: str = DEFAULT_MCP_ENDPOINT,
    apply: bool = False,
) -> tuple[StarterTool, ...]:
    """Install in the caller's transaction; preserve existing definitions and grants.

    The audit receipt records that initial provisioning was attempted, including
    when an existing read grant already satisfied it. Its survival independently
    of an access grant is what prevents revoked grants from reappearing.
    """
    catalog = starter_catalog(mcp_endpoint)
    if not apply:
        return catalog
    await session.execute(text("SELECT pg_advisory_xact_lock(8042261024)"))
    previous = (await session.execute(
        text("SELECT current_setting('app.current_org_id', true)")
    )).scalar_one_or_none()
    try:
        for org_id in dict.fromkeys(organization_ids):
            await skill_grant_provisioning_service._set_org_context(session, str(org_id))
            if await session.get(Organization, org_id) is None:
                raise ValueError(f"organization does not exist or is not visible: {org_id}")
        await skill_grant_provisioning_service._set_org_context(session, "")
        existing = {}
        for tool in catalog:
            skill = await session.get(Skill, tool.id)
            if skill is not None:
                _assert_owned(skill, tool)
                existing[tool.id] = skill
        created = []
        for tool in catalog:
            if tool.id in existing:
                continue
            inserted = (await session.execute(
                insert(Skill).values(**tool.values).on_conflict_do_nothing(
                    index_elements=[Skill.id]).returning(Skill.id)
            )).scalar_one_or_none()
            if inserted is None:
                # A tenant row hidden by RLS must never be adopted or overwritten.
                raise ValueError(f"starter tool identity collision: {tool.id}")
            created.append(tool)
        for tool in created:
            if tool.bundle is not None:
                await storage.put_bundle(tool.values["bundle_storage_key"], tool.bundle)
        for org_id in dict.fromkeys(organization_ids):
            await skill_grant_provisioning_service._set_org_context(session, str(org_id))
            for tool in catalog:
                receipt_id = uuid5(STARTER_NAMESPACE, f"grant:{org_id}:{tool.id}")
                receipt = await session.get(AuthorizationAuditEvent, receipt_id)
                if receipt is not None:
                    if (receipt.org_id != org_id or receipt.action != "starter_provision"
                            or receipt.resource_type != "skill"
                            or receipt.resource_id != str(tool.id)
                            or receipt.actor_id != STARTER_NAMESPACE):
                        raise ValueError("starter grant receipt identity collision")
                    continue
                grant_state = [{
                    "principal_type": "organization", "principal_id": str(org_id),
                    "permission": "read",
                }]
                had_read = (await session.execute(select(AccessGrant.id).where(
                    AccessGrant.org_id == org_id,
                    AccessGrant.resource_type == "skill",
                    AccessGrant.resource_id == str(tool.id),
                    AccessGrant.principal_type == "organization",
                    AccessGrant.principal_id == org_id,
                    AccessGrant.permission == "read",
                ))).scalar_one_or_none() is not None
                await skill_grant_provisioning_service._insert_organization_grants(
                    session, org_id, (tool.id,))
                session.add(AuthorizationAuditEvent(
                    id=receipt_id, org_id=org_id, actor_id=STARTER_NAMESPACE,
                    action="starter_provision", resource_type="skill", resource_id=str(tool.id),
                    before_state=grant_state if had_read else [], after_state=grant_state,
                    correlation_id=f"starter-tools-v1:{org_id}:{tool.id}",
                ))
                await session.flush()
        return catalog
    finally:
        await skill_grant_provisioning_service._set_org_context(session, previous)
