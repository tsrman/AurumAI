"""Prepare one historical attachment only in the fixed disposable lifecycle project."""

import json
import os
from pathlib import Path
import subprocess
from uuid import UUID, uuid4
import hashlib

import boto3
from botocore.exceptions import ClientError
import httpx

STATE = Path("/tmp/phaseb-lifecycle-acceptance/archive-fixture.json")
PROJECT = "phaseb-lifecycle-test"
PAYLOAD = ("Lifecycle archive acceptance attachment.\n" + "\n".join(
    f"Historical fact {index:03d}: the saffron archive preserves original files, identifiers and stored vectors."
    for index in range(120)
)).encode()

BUSINESS = """
import asyncio,json,sys
from datetime import datetime
from uuid import UUID
from sqlalchemy import select,text
from src.core.database import async_session,engine
from src.models.conversation import Conversation
from src.models.skill import Skill
from src.models.file_metadata import FileMetadata
from src.models.knowledge_base import KnowledgeBase
from src.models.organization import Organization
from src.models.user import User
from src.models.workflow_definition import WorkflowDefinition
from src.services.conversation_attachments import _get_or_create_conversation_knowledge_base

async def main():
    data=json.loads(sys.argv[1])
    org,conversation=UUID(data['org_id']),UUID(data['conversation_id'])
    engine.echo=False
    engine.sync_engine.hide_parameters=True
    try:
        async with async_session() as session:
            session.info['org_id']=str(org)
            if await session.scalar(text('SELECT current_user')) != 'aurum_ai_app':
                raise RuntimeError('ordinary_app_role_required')
            payload=await session.scalar(select(Conversation).where(Conversation.id==conversation))
            if payload is None:
                raise RuntimeError('active_fixture_required')
            attachment=data.get('attachment')
            if attachment:
                kb=await session.scalar(select(KnowledgeBase).where(KnowledgeBase.id==UUID(attachment['knowledge_base_id']),KnowledgeBase.owning_conversation_id==conversation))
                skill=await session.scalar(select(Skill).where(Skill.id==UUID(attachment['skill_id']),Skill.owning_conversation_id==conversation))
            else:
                kb=await _get_or_create_conversation_knowledge_base(session,payload)
                skill=await session.scalar(select(Skill).where(Skill.owning_conversation_id==conversation))
            if skill is None or kb is None:
                raise RuntimeError('owned_attachment_skill_required')
            result={'knowledge_base_id':str(kb.id),'skill_id':str(skill.id)}
            if attachment and attachment.get('document_id'):
                if result['knowledge_base_id'] != attachment['knowledge_base_id'] or result['skill_id'] != attachment['skill_id']:
                    raise RuntimeError('established_attachment_owner_mismatch')
                row=await session.get(FileMetadata,UUID(attachment['file_metadata_id']))
                expected={
                    'org_id':org,'owning_conversation_id':conversation,
                    'storage_key':attachment['source_key'],'filename':attachment['filename'],
                    'content_type':'text/plain','size_bytes':attachment['size'],
                    'uploaded_by':UUID(data['user_id']),'document_id':UUID(attachment['document_id']),
                    'created_at':datetime.fromisoformat(data['created_at']),
                    'updated_at':datetime.fromisoformat(data['created_at']),
                }
                if row is None:
                    session.add(FileMetadata(id=UUID(attachment['file_metadata_id']),**expected))
                elif any(getattr(row,key)!=value for key,value in expected.items()):
                    raise RuntimeError('established_file_metadata_mismatch')
                await session.commit()
            print(json.dumps(result))
    finally:
        await engine.dispose()
try:
    asyncio.run(main())
except Exception:
    raise SystemExit('Owned attachment metadata preparation failed') from None
"""


def validate_attachment(state):
    attachment = state["attachment"]
    org_id = str(UUID(state["org_id"]))
    kb_id = str(UUID(attachment["knowledge_base_id"]))
    for field in ("skill_id", "operation_id", "file_metadata_id"):
        UUID(attachment[field])
    prefix = f"documents/{org_id}/{kb_id}/"
    key = attachment["source_key"]
    if not key.startswith(prefix) or not key.endswith(".txt"):
        raise ValueError("source_scope_mismatch")
    UUID(key[len(prefix):-4])
    if attachment["filename"] != "lifecycle-archive-attachment.txt":
        raise ValueError("fixture_filename_mismatch")


def owned(service):
    result = subprocess.run(["docker", "inspect", f"{PROJECT}-{service}-1"], capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)[0]
    labels = info["Config"]["Labels"]
    if labels.get("com.docker.compose.project") != PROJECT or labels.get("com.docker.compose.service") != service:
        raise ValueError("service_ownership_mismatch")
    if info.get("State", {}).get("Running") is not True:
        raise ValueError("owned_service_not_running")
    bindings = {"postgres": ("5432/tcp", "55434"), "minio": ("9000/tcp", "59000"),
                "rag-api": ("8110/tcp", "18110")}
    if service in bindings:
        port, host_port = bindings[service]
        if info["NetworkSettings"]["Ports"].get(port) != [{"HostIp": "127.0.0.1", "HostPort": host_port}]:
            raise ValueError("unexpected_owned_service_binding")
    return info


def save(state):
    temporary = STATE.with_suffix(".attachment-tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(state, output)
    os.replace(temporary, STATE)


def business(backend, state):
    result = subprocess.run(["docker", "exec", "-i", backend, "python", "-", json.dumps(state)],
                            input=BUSINESS, capture_output=True, text=True, check=True)
    return json.loads(result.stdout.splitlines()[-1])


def main():
    state = json.loads(STATE.read_text())
    if owned("postgres")["Id"] != state["postgres_container_id"]:
        raise ValueError("established_fixture_mismatch")
    owned("minio")
    backend = owned("backend")["Id"]
    rag = owned("rag-api")
    if rag["NetworkSettings"]["Ports"]["8110/tcp"] != [{"HostIp": "127.0.0.1", "HostPort": "18110"}]:
        raise ValueError("unexpected_rag_binding")
    with httpx.Client(base_url="http://127.0.0.1:18110", timeout=30, trust_env=False) as client:
        schema = client.get("/openapi.json")
        schema.raise_for_status()
        if "operation_id" not in schema.json()["components"]["schemas"]["DocumentCreate"]["properties"]:
            raise ValueError("reviewed_durable_rag_api_required")
        if "attachment" not in state:
            ids = business(backend, state)
            state["attachment"] = {
                **ids, "operation_id": str(uuid4()), "file_metadata_id": str(uuid4()),
                "source_key": f"documents/{state['org_id']}/{ids['knowledge_base_id']}/{uuid4()}.txt",
                "filename": "lifecycle-archive-attachment.txt", "size": len(PAYLOAD),
                "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "source_uploaded": False,
            }
            save(state)
        attachment = state["attachment"]
        validate_attachment(state)
        # Check established parent ownership before any external mutation, also
        # when resuming a journal after a lost API reply.
        business(backend, state)
        if attachment["sha256"] != hashlib.sha256(PAYLOAD).hexdigest():
            raise ValueError("fixture_source_changed")
        storage = boto3.client("s3", endpoint_url="http://127.0.0.1:59000",
                               aws_access_key_id="lifecycle-business", aws_secret_access_key="lifecycle-business-password",
                               region_name="us-east-1")
        try:
            obj = storage.get_object(Bucket="lifecycle-business", Key=attachment["source_key"])
        except ClientError as error:
            if error.response["Error"]["Code"] not in {"NoSuchKey", "404"} or attachment["source_uploaded"]:
                raise ValueError("established_source_unavailable") from None
            storage.put_object(Bucket="lifecycle-business", Key=attachment["source_key"], Body=PAYLOAD,
                               ContentType="text/plain", IfNoneMatch="*")
        else:
            try:
                if obj["Body"].read(len(PAYLOAD) + 1) != PAYLOAD:
                    raise ValueError("source_key_collision")
            finally:
                obj["Body"].close()
        attachment["source_uploaded"] = True
        save(state)
        response = client.post("/v1/documents", headers={"X-Org-Id": state["org_id"],
                               "X-Knowledge-Base-Id": attachment["knowledge_base_id"]}, json={
                                   "knowledge_base_id": attachment["knowledge_base_id"],
                                   "title": "Lifecycle archive attachment", "source_key": attachment["source_key"],
                                   "source_filename": attachment["filename"], "operation_id": attachment["operation_id"],
                               })
        response.raise_for_status()
        document = response.json()
        document_id = str(UUID(document["id"]))
        if str(UUID(document["indexing_operation_id"])) != attachment["operation_id"]:
            raise ValueError("index_operation_identity_changed")
        if attachment.get("document_id", document_id) != document_id:
            raise ValueError("idempotent_document_identity_changed")
        attachment.update(document_id=document_id, generation=document["generation"],
                          indexing_operation_id=str(UUID(document["indexing_operation_id"])),
                          workflow_id=f"rag-index-{document_id}-{attachment['operation_id']}")
        save(state)
        business(backend, state)
    print("Owned historical attachment admitted with durable indexing operation; wait for actual indexing/closure")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Owned attachment preparation blocked; preserve journal and reconcile exact operation") from None
