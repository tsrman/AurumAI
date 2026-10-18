#!/usr/bin/env python3
"""Guarded fixture lifecycle for the real Knowledge/RAG browser test.

Only ownership metadata is persisted. Credentials and bearer tokens stay in
memory, and every fallback deletion is constrained by an organization UUID and
an exact fixture ID or allowlisted fixture name.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields, replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Sequence
import urllib.error
import urllib.parse
import urllib.request
import uuid


OWNED_MARKER = re.compile(r"^knowledge-ui-e2e-[a-f0-9]{10,32}$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
CREATE_CAPABILITY = "workspace.knowledge.create"


class FixtureError(RuntimeError):
    """Safe operational error that never includes response bodies or secrets."""


@dataclass(frozen=True)
class Config:
    business_url: str = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    admin_url: str = os.getenv("ADMIN_BASE_URL", "http://localhost:8001").rstrip("/")
    keycloak_url: str = os.getenv("KEYCLOAK_URL", "http://localhost:8080").rstrip("/")
    realm: str = os.getenv("KEYCLOAK_REALM", "aurum-ai")
    business_client: str = os.getenv("KEYCLOAK_CLIENT_ID", "business-ui")
    admin_client: str = os.getenv("ADMIN_KEYCLOAK_CLIENT_ID", "admin-ui")
    member_username: str = os.getenv("E2E_MEMBER_USERNAME", "user")
    member_password: str = os.getenv("E2E_MEMBER_PASSWORD", "user")
    admin_username: str = os.getenv("E2E_ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("E2E_ADMIN_PASSWORD", "admin")
    postgres_container: str = os.getenv("E2E_POSTGRES_CONTAINER", "aurum-ai-postgres")
    minio_container: str = os.getenv("E2E_MINIO_CONTAINER", "aurum-ai-minio")
    rag_api_container: str = os.getenv("E2E_RAG_API_CONTAINER", "aurum-ai-rag-api")
    business_database: str = os.getenv("POSTGRES_DB", "aurum_ai")
    rag_database: str = os.getenv("RAG_DATABASE", "rag_platform")
    postgres_user: str = os.getenv("POSTGRES_USER", "postgres")
    bucket: str = os.getenv("S3_BUCKET", "aurum-ai-business")


@dataclass
class FixtureState:
    marker: str
    organization_id: str = ""
    member_id: str = ""
    original_capabilities: list[str] | None = None
    knowledge_base_name: str = ""
    source_filename: str = ""
    knowledge_base_id: str = ""
    document_id: str = ""
    storage_key: str = ""


def _valid_uuid(value: str, field_name: str, *, required: bool = False) -> None:
    if not value:
        if required:
            raise FixtureError(f"{field_name} is required")
        return
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise FixtureError(f"invalid {field_name}") from exc
    if str(parsed) != value.lower():
        raise FixtureError(f"invalid {field_name}")


def validate_marker(marker: str) -> None:
    if not OWNED_MARKER.fullmatch(marker):
        raise FixtureError("invalid ownership marker")


def validate_owned_state(state: FixtureState, *, require_resources: bool = False) -> None:
    validate_marker(state.marker)
    _valid_uuid(state.organization_id, "organization_id", required=True)
    _valid_uuid(state.member_id, "member_id", required=True)
    _valid_uuid(state.knowledge_base_id, "knowledge_base_id")
    _valid_uuid(state.document_id, "document_id")

    expected_base = f"{state.marker}-base"
    expected_source = f"{state.marker}.txt"
    if state.knowledge_base_name != expected_base:
        raise FixtureError("invalid knowledge_base_name")
    if state.source_filename != expected_source:
        raise FixtureError("invalid source_filename")
    if require_resources and not (state.knowledge_base_id or state.knowledge_base_name):
        raise FixtureError("owned knowledge-base identity is required")
    if state.document_id and not state.knowledge_base_id:
        raise FixtureError("document_id requires knowledge_base_id")
    if state.storage_key:
        if not state.knowledge_base_id or not state.document_id:
            raise FixtureError("storage_key requires captured resource IDs")
        prefix = (
            f"documents/{state.organization_id}/{state.knowledge_base_id}/"
        )
        if not state.storage_key.startswith(prefix) or ".." in state.storage_key:
            raise FixtureError("invalid storage_key")
    if state.original_capabilities is None:
        raise FixtureError("original_capabilities are required")
    if any(not CAPABILITY.fullmatch(item) for item in state.original_capabilities):
        raise FixtureError("invalid original_capabilities")
    if state.original_capabilities != sorted(set(state.original_capabilities)):
        raise FixtureError("original_capabilities must be sorted and unique")


def save_state(path: Path, state: FixtureState) -> None:
    validate_owned_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def load_state(path: Path) -> FixtureState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError("could not read fixture state") from exc
    if not isinstance(raw, dict):
        raise FixtureError("fixture state must be a JSON object")
    allowed = {item.name for item in fields(FixtureState)}
    unknown = set(raw) - allowed
    if unknown:
        raise FixtureError("unknown state fields")
    try:
        state = FixtureState(**raw)
    except TypeError as exc:
        raise FixtureError("invalid fixture state schema") from exc
    if not all(isinstance(getattr(state, item), str) for item in (
        "marker", "organization_id", "member_id", "knowledge_base_name",
        "source_filename", "knowledge_base_id", "document_id", "storage_key",
    )):
        raise FixtureError("invalid fixture state value types")
    if state.original_capabilities is not None and (
        not isinstance(state.original_capabilities, list)
        or not all(isinstance(item, str) for item in state.original_capabilities)
    ):
        raise FixtureError("invalid original_capabilities")
    validate_owned_state(state)
    return state


def primary_exit_code(primary: int, cleanup: int) -> int:
    return primary if primary else cleanup


class FixtureController:
    def __init__(
        self,
        config: Config,
        *,
        state_path: Path,
        marker: str,
    ) -> None:
        validate_marker(marker)
        self.config = config
        self.state_path = state_path
        self.marker = marker

    def _run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def _request(
        self,
        plane: str,
        method: str,
        route: str,
        *,
        token: str | None = None,
        form: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if plane == "business":
            url = f"{self.config.business_url}{route}"
        elif plane == "admin":
            url = f"{self.config.admin_url}{route}"
        elif plane == "keycloak":
            url = f"{self.config.keycloak_url}{route}"
        else:
            raise FixtureError("invalid HTTP plane")
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(form).encode()
        elif payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode()
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status = response.status
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_body = b""
        except OSError as exc:
            raise FixtureError(f"{plane} {method} {route}: transport failed") from exc
        if status not in expected:
            raise FixtureError(f"{plane} {method} {route}: HTTP {status}")
        if not response_body:
            return None
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{plane} {method} {route}: invalid JSON") from exc

    def _token(self, username: str, password: str, client_id: str) -> str:
        body = self._request(
            "keycloak",
            "POST",
            f"/realms/{self.config.realm}/protocol/openid-connect/token",
            form={
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
            },
        )
        try:
            return str(body["access_token"])
        except (KeyError, TypeError) as exc:
            raise FixtureError("keycloak token response is invalid") from exc

    def _auth_me(self, plane: str, username: str, password: str) -> dict[str, object]:
        client = self.config.business_client if plane == "business" else self.config.admin_client
        token = self._token(username, password, client)
        body = self._request(plane, "GET", "/v1/auth/me", token=token)
        try:
            data = body["data"]
        except (KeyError, TypeError) as exc:
            raise FixtureError(f"{plane} GET /v1/auth/me: invalid response") from exc
        if not isinstance(data, dict):
            raise FixtureError(f"{plane} GET /v1/auth/me: invalid response")
        return data

    def _admin_token(self) -> str:
        return self._token(
            self.config.admin_username,
            self.config.admin_password,
            self.config.admin_client,
        )

    def _business_admin_token(self) -> str:
        return self._token(
            self.config.admin_username,
            self.config.admin_password,
            self.config.business_client,
        )

    def _get_capabilities(self) -> list[str]:
        body = self._request(
            "admin",
            "GET",
            "/v1/admin/authorization/capabilities/organization",
            token=self._admin_token(),
        )
        try:
            capabilities = body["data"]["capabilities"]
        except (KeyError, TypeError) as exc:
            raise FixtureError("admin capability response is invalid") from exc
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise FixtureError("admin capability response is invalid")
        return sorted(set(capabilities))

    def _put_capabilities(self, capabilities: list[str]) -> None:
        self._request(
            "admin",
            "PUT",
            "/v1/admin/authorization/capabilities/organization",
            token=self._admin_token(),
            payload={"capabilities": capabilities},
        )

    def _database(self, database: str, sql: str) -> str:
        result = self._run(
            [
                "docker", "exec", "-i", self.config.postgres_container,
                "psql", "-X", "-v", "ON_ERROR_STOP=1", "-At",
                "-U", self.config.postgres_user, "-d", database, "-c", sql,
            ]
        )
        if result.returncode != 0:
            raise FixtureError("database command failed")
        return result.stdout.strip()

    def _business_scalar(self, sql: str) -> str:
        return self._database(self.config.business_database, sql)

    def _rag_scalar(self, sql: str) -> str:
        return self._database(self.config.rag_database, sql)

    def _business_execute(self, sql: str) -> None:
        self._database(self.config.business_database, sql)

    def _rag_execute(self, sql: str) -> None:
        self._database(self.config.rag_database, sql)

    @staticmethod
    def _count(value: str, label: str) -> int:
        try:
            return int(value or "0")
        except ValueError as exc:
            raise FixtureError(f"invalid {label} count") from exc

    def prepare(self) -> None:
        if self.state_path.exists():
            raise FixtureError("fixture state already exists")
        business_identity = self._auth_me(
            "business", self.config.member_username, self.config.member_password
        )
        admin_identity = self._auth_me(
            "admin", self.config.member_username, self.config.member_password
        )
        organization_id = str(business_identity.get("org_id") or "")
        member_id = str(business_identity.get("user_id") or "")
        if (
            str(admin_identity.get("org_id") or "") != organization_id
            or str(admin_identity.get("user_id") or "") != member_id
        ):
            raise FixtureError("business/admin identity mismatch")
        original_capabilities = sorted(set(self._get_capabilities()))
        if CREATE_CAPABILITY in original_capabilities:
            raise FixtureError(
                "organization already has workspace.knowledge.create; "
                "the negative UI precondition would be invalid"
            )
        state = FixtureState(
            marker=self.marker,
            organization_id=organization_id,
            member_id=member_id,
            original_capabilities=original_capabilities,
            knowledge_base_name=f"{self.marker}-base",
            source_filename=f"{self.marker}.txt",
        )
        validate_owned_state(state)
        base_collision = self._count(
            self._business_scalar(
                "SELECT count(*) FROM knowledge_bases "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND name = '{state.knowledge_base_name}';"
            ),
            "knowledge-base collision",
        )
        file_collision = self._count(
            self._business_scalar(
                "SELECT count(*) FROM file_metadata "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND filename = '{state.source_filename}';"
            ),
            "file collision",
        )
        if base_collision or file_collision:
            raise FixtureError("exact fixture collision detected")
        save_state(self.state_path, state)

    def capture(self, knowledge_base_id: str, document_id: str) -> None:
        state = load_state(self.state_path)
        if state.marker != self.marker:
            raise FixtureError("marker does not match fixture state")
        _valid_uuid(knowledge_base_id, "knowledge_base_id", required=True)
        _valid_uuid(document_id, "document_id", required=True)
        base = self._business_scalar(
            "SELECT id::text FROM knowledge_bases "
            f"WHERE id = '{knowledge_base_id}' AND org_id = '{state.organization_id}' "
            f"AND name = '{state.knowledge_base_name}';"
        )
        if base != knowledge_base_id:
            raise FixtureError("owned knowledge base was not found")
        document = self._rag_scalar(
            "SELECT id::text FROM documents "
            f"WHERE id = '{document_id}' AND org_id = '{state.organization_id}' "
            f"AND knowledge_base_id = '{knowledge_base_id}' "
            f"AND source_filename = '{state.source_filename}';"
        )
        if document != document_id:
            raise FixtureError("owned RAG document relationship was not found")
        storage_key = self._business_scalar(
            "SELECT storage_key FROM file_metadata "
            f"WHERE org_id = '{state.organization_id}' AND document_id = '{document_id}' "
            f"AND filename = '{state.source_filename}';"
        )
        captured = replace(
            state,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            storage_key=storage_key,
        )
        validate_owned_state(captured, require_resources=True)
        save_state(self.state_path, captured)

    def _resolve_owned_resources(self, state: FixtureState) -> FixtureState:
        knowledge_base_id = state.knowledge_base_id
        if not knowledge_base_id:
            knowledge_base_id = self._business_scalar(
                "SELECT id::text FROM knowledge_bases "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND name = '{state.knowledge_base_name}' LIMIT 1;"
            )
        if knowledge_base_id:
            _valid_uuid(knowledge_base_id, "knowledge_base_id", required=True)
        document_id = state.document_id
        if knowledge_base_id and not document_id:
            document_id = self._rag_scalar(
                "SELECT id::text FROM documents "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND knowledge_base_id = '{knowledge_base_id}' "
                f"AND source_filename = '{state.source_filename}' LIMIT 1;"
            )
        if document_id:
            _valid_uuid(document_id, "document_id", required=True)
        storage_key = state.storage_key
        if document_id and not storage_key:
            storage_key = self._business_scalar(
                "SELECT storage_key FROM file_metadata "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND document_id = '{document_id}' "
                f"AND filename = '{state.source_filename}' LIMIT 1;"
            )
        resolved = replace(
            state,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            storage_key=storage_key,
        )
        validate_owned_state(resolved, require_resources=True)
        if resolved != state:
            save_state(self.state_path, resolved)
        return resolved

    def _delete_document_via_business_api(self, state: FixtureState) -> None:
        if not state.document_id or not state.knowledge_base_id:
            return
        route = (
            f"/v1/documents/{state.document_id}?knowledge_base_id="
            f"{state.knowledge_base_id}"
        )
        self._request(
            "business", "DELETE", route,
            token=self._business_admin_token(), expected=(204, 404),
        )

    def _delete_knowledge_base_via_business_api(self, state: FixtureState) -> None:
        if not state.knowledge_base_id:
            return
        self._request(
            "business", "DELETE", f"/v1/knowledge-bases/{state.knowledge_base_id}",
            token=self._business_admin_token(), expected=(204, 404),
        )

    def _delete_minio_object(self, key: str) -> None:
        if not self._minio_exists(key):
            return
        result = self._run(
            [
                "docker", "exec", self.config.minio_container,
                "mc", "rm", "--force", f"local/{self.config.bucket}/{key}",
            ]
        )
        if result.returncode != 0 and not self._minio_result_is_missing(result):
            raise FixtureError("exact MinIO cleanup failed")

    @staticmethod
    def _minio_result_is_missing(result: subprocess.CompletedProcess[str]) -> bool:
        output = f"{result.stdout}\n{result.stderr}".lower()
        return any(
            marker in output
            for marker in ("object does not exist", "nosuchkey", "no such key")
        )

    def _minio_exists(self, key: str) -> bool:
        result = self._run(
            [
                "docker", "exec", self.config.minio_container,
                "mc", "stat", f"local/{self.config.bucket}/{key}",
            ]
        )
        if result.returncode == 0:
            return True
        if self._minio_result_is_missing(result):
            return False
        raise FixtureError("exact MinIO stat failed")

    def _milvus_command(self, operation: str, document_id: str, knowledge_base_id: str) -> int:
        script = (
            "import sys; from pymilvus import MilvusClient; "
            "c=MilvusClient(uri='http://milvus:19530'); n='documents'; "
            "f='document_id == \\\"%s\\\" and knowledge_base_id == \\\"%s\\\"' % (sys.argv[1], sys.argv[2]); "
            "exists=c.has_collection(n); "
            + (
                "c.delete(collection_name=n, filter=f) if exists else None; print(0)"
                if operation == "delete"
                else "print(len(c.query(collection_name=n, filter=f, output_fields=['pk'], limit=1)) if exists else 0)"
            )
        )
        result = self._run(
            [
                "docker", "exec", self.config.rag_api_container,
                "python", "-c", script, document_id, knowledge_base_id,
            ]
        )
        if result.returncode != 0:
            raise FixtureError(f"exact Milvus {operation} failed")
        return self._count(result.stdout.strip(), "Milvus")

    def _delete_milvus_chunks(self, document_id: str, knowledge_base_id: str) -> None:
        self._milvus_command("delete", document_id, knowledge_base_id)

    def _milvus_count(self, document_id: str, knowledge_base_id: str) -> int:
        return self._milvus_command("count", document_id, knowledge_base_id)

    def _cleanup_exact_fallbacks(self, state: FixtureState) -> None:
        validate_owned_state(state, require_resources=True)
        if state.storage_key:
            self._delete_minio_object(state.storage_key)
        if state.document_id and state.knowledge_base_id:
            self._delete_milvus_chunks(state.document_id, state.knowledge_base_id)
            self._rag_execute(
                "DELETE FROM documents "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND id = '{state.document_id}' "
                f"AND knowledge_base_id = '{state.knowledge_base_id}' "
                f"AND source_filename = '{state.source_filename}';"
            )
            self._business_execute(
                "DELETE FROM file_metadata "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND document_id = '{state.document_id}' "
                f"AND filename = '{state.source_filename}';"
            )
        if state.knowledge_base_id:
            self._business_execute(
                "DELETE FROM access_grants "
                f"WHERE org_id = '{state.organization_id}' "
                "AND resource_type = 'knowledge_base' "
                f"AND resource_id = '{state.knowledge_base_id}';"
            )
            self._business_execute(
                "DELETE FROM knowledge_bases "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND id = '{state.knowledge_base_id}' "
                f"AND name = '{state.knowledge_base_name}';"
            )

    def _restore_original_capabilities(self, state: FixtureState) -> None:
        capabilities = state.original_capabilities or []
        try:
            self._put_capabilities(capabilities)
            return
        except FixtureError:
            pass
        for capability in capabilities:
            self._business_execute(
                "INSERT INTO organization_capabilities (org_id, capability) "
                f"VALUES ('{state.organization_id}', '{capability}') "
                "ON CONFLICT DO NOTHING;"
            )
        if CREATE_CAPABILITY not in capabilities:
            self._business_execute(
                "DELETE FROM organization_capabilities "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND capability = '{CREATE_CAPABILITY}';"
            )

    def _current_capabilities(self, state: FixtureState) -> list[str]:
        try:
            return self._get_capabilities()
        except FixtureError:
            value = self._business_scalar(
                "SELECT coalesce(string_agg(capability, ',' ORDER BY capability), '') "
                "FROM organization_capabilities "
                f"WHERE org_id = '{state.organization_id}';"
            )
            return [] if not value else value.split(",")

    def verify_clean(self, state: FixtureState | None = None) -> None:
        state = state or load_state(self.state_path)
        validate_owned_state(state, require_resources=True)
        problems: list[str] = []
        base_count = self._count(
            self._business_scalar(
                "SELECT count(*) FROM knowledge_bases "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND name = '{state.knowledge_base_name}';"
            ), "knowledge-base",
        )
        metadata_count = self._count(
            self._business_scalar(
                "SELECT count(*) FROM file_metadata "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND filename = '{state.source_filename}';"
            ), "file metadata",
        )
        rag_count = self._count(
            self._rag_scalar(
                "SELECT count(*) FROM documents "
                f"WHERE org_id = '{state.organization_id}' "
                f"AND source_filename = '{state.source_filename}';"
            ), "RAG document",
        )
        if base_count:
            problems.append("knowledge base remains")
        if metadata_count:
            problems.append("file metadata remains")
        if rag_count:
            problems.append("RAG document remains")
        if state.storage_key and self._minio_exists(state.storage_key):
            problems.append("MinIO object remains")
        if state.document_id and state.knowledge_base_id and self._milvus_count(
            state.document_id, state.knowledge_base_id
        ):
            problems.append("Milvus chunks remain")
        if self._current_capabilities(state) != (state.original_capabilities or []):
            problems.append("organization capabilities were not restored")
        if problems:
            raise FixtureError("verification failed: " + "; ".join(problems))

    def cleanup(self) -> None:
        state = load_state(self.state_path)
        if state.marker != self.marker:
            raise FixtureError("marker does not match fixture state")
        validate_owned_state(state, require_resources=True)
        try:
            state = self._resolve_owned_resources(state)
        except FixtureError as exc:
            print(f"[knowledge-ui-e2e] resource discovery unavailable: {exc}", file=sys.stderr)
        for action in (
            self._delete_document_via_business_api,
            self._delete_knowledge_base_via_business_api,
        ):
            try:
                action(state)
            except FixtureError as exc:
                print(f"[knowledge-ui-e2e] public cleanup unavailable: {exc}", file=sys.stderr)
        failures: list[str] = []
        for action in (
            self._cleanup_exact_fallbacks,
            self._restore_original_capabilities,
            self.verify_clean,
        ):
            try:
                action(state)
            except FixtureError as exc:
                failures.append(str(exc))
        if failures:
            raise FixtureError("; ".join(failures))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", required=True, type=Path)
    common.add_argument("--marker", required=True)
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare", parents=[common])
    capture = subparsers.add_parser("capture", parents=[common])
    capture.add_argument("--knowledge-base-id", required=True)
    capture.add_argument("--document-id", required=True)
    subparsers.add_parser("cleanup", parents=[common])
    subparsers.add_parser("verify-clean", parents=[common])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        controller = FixtureController(
            Config(), state_path=args.state, marker=args.marker
        )
        if args.command == "prepare":
            controller.prepare()
        elif args.command == "capture":
            controller.capture(args.knowledge_base_id, args.document_id)
        elif args.command == "cleanup":
            controller.cleanup()
        else:
            controller.verify_clean()
        return 0
    except FixtureError as exc:
        print(f"[knowledge-ui-e2e] FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
