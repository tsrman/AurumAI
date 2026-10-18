# ruff: noqa: F811
"""Independent whole-graph SQL round trip and nonempty retained billing proof."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import text
import pytest
from tests.services.test_business_archive_service import archive_fixture, LocalArchiveStorage  # noqa:F401
from src.services.archive.service import ArchiveService


async def test_multitable_graph_roundtrip_and_retained_billing(archive_fixture, tmp_path):
    sessions, org, conversation = archive_fixture
    agent, definition, run, node, turn, execution, operation, question, outbox, webop, source = [
        uuid4() for _ in range(11)
    ]
    message_ids = [uuid4() for _ in range(260)]
    at = datetime.now(timezone.utc) - timedelta(days=181)
    args = dict(
        o=org,
        c=conversation,
        a=agent,
        d=definition,
        r=run,
        n=node,
        t=turn,
        e=execution,
        op=operation,
        q=question,
        b=outbox,
        w=webop,
        s=source,
        at=at,
        m=message_ids[0],
    )
    async with sessions() as db:
        args["u"] = await db.scalar(text("SELECT user_id FROM conversations WHERE id=:c"), args)
        await db.execute(text("SELECT platform_ensure_partition('messages',:at)"), args)
        await db.execute(
            text(
                "INSERT INTO messages(id,org_id,conversation_id,user_id,role,content,created_at,updated_at,cost_usd) VALUES(:id,:o,:c,:u,'assistant',:body,:at,:at,0.000001)"
            ),
            [
                dict(args, id=value, body=f"history {index}")
                for index, value in enumerate(message_ids)
            ],
        )
        await db.execute(
            text(
                "INSERT INTO agent_definitions(id,org_id,name,model_id,system_prompt) VALUES(:a,:o,'agent','test','prompt')"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO workflow_definitions(id,org_id,name,supervisor_agent_definition_id) VALUES(:d,:o,'workflow',:a)"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO workflow_runs(id,org_id,conversation_id,initiating_user_id,workflow_definition_id,workflow_definition_version,turn_id,source_message_id,temporal_workflow_id,execution_snapshot,status,created_at,updated_at) VALUES(:r,:o,:c,:u,:d,1,:t,:m,:wf,'{}','completed',:at,:at)"
            ),
            dict(args, wf=f"archive-test-{run}"),
        )
        await db.execute(
            text(
                "INSERT INTO workflow_node_runs(id,org_id,workflow_run_id,phase_key,agent_definition_id,agent_name,status,output,internal_error,diagnostic_expires_at) VALUES(:n,:o,:r,'node:a',:a,'agent','completed','temporary diagnostic','private diagnostic',:at)"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,workflow_run_id,status,limits,terminal_at) VALUES(:t,:o,:c,:u,:r,'completed','{}',:at)"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO interaction_executions(id,org_id,turn_id,workflow_id,run_id,task_queue,phase) VALUES(:e,:o,:t,:wf,:r,'test','ordinary')"
            ),
            dict(args, wf=f"archive-test-{run}"),
        )
        await db.execute(
            text(
                "INSERT INTO tool_operations(id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash,status) VALUES(:op,:o,:t,:e,:op,'ask_user',:hash,'completed')"
            ),
            dict(args, hash="a" * 64),
        )
        await db.execute(
            text(
                "INSERT INTO user_questions(id,org_id,turn_id,execution_id,operation_id,text,status,expires_at,created_at,answer,answered_at,answered_by,answer_key,consumed_at) VALUES(:q,:o,:t,:e,:op,'Question?','answered',:deadline,:at,'{\"text\":\"answer\"}',:at,:u,:q,:at)"
            ),
            dict(args, deadline=at + timedelta(hours=1)),
        )
        await db.execute(
            text(
                "INSERT INTO interaction_outbox(id,org_id,turn_id,execution_id,question_id,question_version,event_kind,status) VALUES(:b,:o,:t,:e,:q,1,'question_available','delivered')"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO tool_operations(id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash,status,result) VALUES(:w,:o,:t,:e,:w,'web_read',:hash,'completed','{\"body\":\"transient\"}')"
            ),
            dict(args, hash="b" * 64),
        )
        await db.execute(
            text(
                "INSERT INTO web_sources(id,org_id,turn_id,execution_id,operation_id,position,kind,final_url,content) VALUES(:s,:o,:t,:e,:w,0,'read','https://example.com','transient body')"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO interaction_outbox(id,org_id,turn_id,execution_id,question_id,question_version,event_kind,status) VALUES(:id,:o,:t,:e,:q,1,'question_status','pending')"
            ),
            dict(args, id=uuid4()),
        )
        # Authoritative receipt fixture for the SQL graph test; real Temporal observer
        # transport/history is tested separately, never inferred by this test.
        await db.execute(
            text(
                "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete) VALUES(:id,:o,:c,1,'default',:wf,:r,'ConversationWorkflow','test','COMPLETED',:at,:at,true)"
            ),
            dict(args, id=uuid4(), wf=f"archive-test-{run}"),
        )
        await db.execute(
            text(
                "INSERT INTO llm_usage_events(id,org_id,user_id,conversation_id,request_id,workflow_run_id,workflow_phase,workflow_node_key,period_start,model,status,prompt_tokens,completion_tokens,total_tokens) VALUES(:id,:o,:u,:c,:request,:r,'node','a',current_date,'test','completed',7,11,18)"
            ),
            dict(args, id=uuid4(), request=uuid4()),
        )
        await db.execute(
            text(
                "INSERT INTO organization_usage_monthly(org_id,period_start,requests_used,prompt_tokens,completion_tokens,total_tokens) VALUES(:o,current_date,1,7,11,18)"
            ),
            args,
        )
        await db.commit()
        billing = (
            (
                await db.execute(
                    text("SELECT to_jsonb(e) FROM llm_usage_events e WHERE org_id=:o"), args
                )
            )
            .scalars()
            .all()
        )
        totals = (
            (
                await db.execute(
                    text("SELECT to_jsonb(e) FROM organization_usage_monthly e WHERE org_id=:o"),
                    args,
                )
            )
            .scalars()
            .all()
        )
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    result = await service.run(org, conversation, apply=True)
    async with sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM messages WHERE org_id=:o"), args) == 0
        assert (
            await db.scalar(text("SELECT count(*) FROM workflow_runs WHERE org_id=:o"), args) == 0
        )
        assert (
            await db.scalar(text("SELECT count(*) FROM message_identities WHERE org_id=:o"), args)
            == 260
        )
        assert (
            await db.execute(
                text("SELECT to_jsonb(e) FROM llm_usage_events e WHERE org_id=:o"), args
            )
        ).scalars().all() == billing
    await service.restore(org, result["archive_id"], apply=True)
    async with sessions() as db:
        messages = (
            await db.execute(text("SELECT id,content,cost_usd FROM messages WHERE org_id=:o"), args)
        ).all()
        assert (
            await db.scalar(
                text("SELECT count(*) FROM messages WHERE org_id=:o AND tool_calls IS NULL"), args
            )
            == 260
        )
        assert {row.id: row.content for row in messages} == {
            value: f"history {index}" for index, value in enumerate(message_ids)
        }
        for table, count in [
            ("workflow_runs", 1),
            ("workflow_node_runs", 1),
            ("interaction_turns", 1),
            ("interaction_executions", 1),
            ("tool_operations", 2),
            ("user_questions", 1),
            ("interaction_outbox", 2),
            ("web_sources", 1),
        ]:
            assert (
                await db.scalar(text(f"SELECT count(*) FROM {table} WHERE org_id=:o"), args)
                == count
            )
        assert await db.scalar(text("SELECT content FROM web_sources WHERE id=:s"), args) is None
        assert (
            await db.execute(
                text("SELECT output,internal_error FROM workflow_node_runs WHERE id=:n"), args
            )
        ).one() == (None, None)
        assert await db.scalar(text("SELECT answer FROM user_questions WHERE id=:q"), args) == {
            "text": "answer"
        }
        assert (
            await db.scalar(text("SELECT status FROM interaction_outbox WHERE id=:b"), args)
            == "delivered"
        )
        assert (
            await db.scalar(
                text(
                    "SELECT status FROM interaction_outbox WHERE org_id=:o AND event_kind='question_status'"
                ),
                args,
            )
            == "cancelled"
        )
        assert (
            await db.execute(
                text("SELECT to_jsonb(e) FROM llm_usage_events e WHERE org_id=:o"), args
            )
        ).scalars().all() == billing
        assert (
            await db.execute(
                text("SELECT to_jsonb(e) FROM organization_usage_monthly e WHERE org_id=:o"), args
            )
        ).scalars().all() == totals


@pytest.mark.parametrize(
    "collision", [None, "kb_name", "storage_key", "race_kb_name", "race_storage_key"]
)
async def test_attachment_metadata_grants_and_independent_rag_generation(
    archive_fixture, tmp_path, collision
):
    from src.services.rag_archive_client import RagArchiveInspection, RagArchiveReceipt

    sessions, org, conversation = archive_fixture
    kb, skill, file, document, grant = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    source = f"documents/{org}/{kb}/{uuid4()}.txt"
    async with sessions() as db:
        args = dict(o=org, c=conversation, k=kb, s=skill, f=file, d=document, g=grant, key=source)
        args["u"] = await db.scalar(text("SELECT user_id FROM conversations WHERE id=:c"), args)
        await db.execute(
            text(
                "INSERT INTO knowledge_bases(id,org_id,name,owning_conversation_id) VALUES(:k,:o,'hidden',:c)"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO skills(id,org_id,name,type,activity_name,input_schema,owning_conversation_id) VALUES(:s,:o,'hidden','tool','execute_hybrid_search',CAST(:schema AS json),:c)"
            ),
            dict(
                args,
                schema=__import__("json").dumps(
                    {
                        "type": "object",
                        "properties": {
                            "knowledge_base_id": {"type": "string", "const": str(kb)},
                            "query": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["knowledge_base_id", "query"],
                    }
                ),
            ),
        )
        await db.execute(
            text(
                "INSERT INTO file_metadata(id,org_id,owning_conversation_id,storage_key,filename,size_bytes,document_id) VALUES(:f,:o,:c,:key,'fixture.txt',128,:d)"
            ),
            args,
        )
        await db.execute(
            text(
                "INSERT INTO access_grants(id,org_id,resource_type,resource_id,principal_type,principal_id,permission,created_via,created_by) VALUES(:g,:o,'skill',:resource,'user',:u,'read','admin',:u)"
            ),
            dict(args, resource=str(skill)),
        )
        await db.commit()
        grants = (
            await db.scalars(text("SELECT to_jsonb(g) FROM access_grants g WHERE org_id=:o"), args)
        ).all()
    calls = []

    class Rag:
        async def inspect(self, **kwargs):
            return RagArchiveInspection(state="active", generation=7, archive_id=None)

        receipt_hash = None

        async def command(self, action, **kwargs):
            if action == "export":
                import hashlib
                import json
                import pyarrow as pa
                import pyarrow.parquet as pq
                from src.services.archive.rag_artifacts import DOCUMENT_SCHEMA

                prefix = f"archives/v1/{org}/{kwargs['archive_id']}/rag"

                def put(relative, data):
                    path = tmp_path / "objects" / prefix / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    return {
                        "key": prefix + "/" + relative,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }

                sink = pa.BufferOutputStream()
                pq.write_table(
                    pa.Table.from_pylist(
                        [
                            {
                                "id": str(document),
                                "org_id": str(org),
                                "knowledge_base_id": str(kb),
                                "source_key": source,
                                "chunk_count": 0,
                            }
                        ],
                        schema=DOCUMENT_SCHEMA,
                    ),
                    sink,
                )
                meta = put(f"documents/{document}.parquet", sink.getvalue().to_pybytes())
                source_part = put(f"sources/{document}", b"x" * 128)
                source_part["source_key"] = source
                manifest = {
                    "version": 1,
                    "org_id": str(org),
                    "knowledge_base_id": str(kb),
                    "archive_id": str(kwargs["archive_id"]),
                    "generation": 8,
                    "vector_schema": {"dimension": 1536, "sha256": "c" * 64},
                    "documents": [
                        {
                            "id": str(document),
                            "metadata": meta,
                            "vectors": [],
                            "source": source_part,
                        }
                    ],
                }
                self.receipt_hash = put(
                    "manifest.json",
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
                )["sha256"]
            if not kwargs.get("dry_run"):
                calls.append((action, kwargs["generation"]))
            assert kwargs["org_id"] == org and kwargs["knowledge_base_id"] == kb
            return RagArchiveReceipt(
                archive_id=kwargs["archive_id"],
                generation=kwargs["generation"],
                archive_generation=8,
                receipt_hash=None if action == "claim" else self.receipt_hash,
                state={
                    "claim": "claimed",
                    "export": "verified",
                    "purge": "purged",
                    "restore": "restore_pending",
                    "publish": "restored",
                }[action],
                next_restore_generation=9 if action == "purge" else None,
            )

    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"), Rag())
    result = await service.run(org, conversation, apply=True)
    async with sessions() as db:
        for table in ("knowledge_bases", "skills", "file_metadata"):
            assert await db.scalar(text(f"SELECT count(*) FROM {table} WHERE org_id=:o"), args) == 0
        assert (
            await db.scalars(text("SELECT to_jsonb(g) FROM access_grants g WHERE org_id=:o"), args)
        ).all() == grants
    if collision is not None:
        from src.services.conversation_lifecycle import ArchiveBlocked

        duplicate = uuid4()
        kind = collision.removeprefix("race_")
        race = collision.startswith("race_")
        conflict_org = org if kind == "kb_name" else uuid4()
        table = "knowledge_bases" if kind == "kb_name" else "file_metadata"

        async def create_conflict():
            async with sessions() as db:
                if conflict_org != org:
                    await db.execute(
                        text(
                            "INSERT INTO organizations(id,name,slug) VALUES(:id,'owned archive collision',:slug)"
                        ),
                        dict(id=conflict_org, slug=str(conflict_org)),
                    )
                if kind == "kb_name":
                    await db.execute(
                        text("INSERT INTO knowledge_bases(id,org_id,name) VALUES(:id,:o,'hidden')"),
                        dict(id=duplicate, o=conflict_org),
                    )
                else:
                    await db.execute(
                        text(
                            "INSERT INTO file_metadata(id,org_id,storage_key,filename,size_bytes) VALUES(:id,:o,:key,'duplicate.txt',128)"
                        ),
                        dict(id=duplicate, o=conflict_org, key=source),
                    )
                await db.commit()

        original_dependencies = service._dependencies
        if race:

            async def dependencies_then_conflict(*args):
                await original_dependencies(*args)
                await create_conflict()

            service._dependencies = dependencies_then_conflict
        else:
            await create_conflict()
        before_calls = list(calls)
        try:
            for apply in (True,) if race else (False, True):
                with pytest.raises(ArchiveBlocked, match="restore_payload_collision"):
                    await service.restore(org, result["archive_id"], apply=apply)
                assert calls == before_calls
                async with sessions() as db:
                    assert await db.scalar(
                        text("SELECT state FROM archive_jobs WHERE id=:id"),
                        dict(id=result["archive_id"]),
                    ) == ("restore_pending" if race else "purged")
                    identity = (
                        await db.execute(
                            text(
                                "SELECT lifecycle,generation FROM conversation_identities WHERE id=:id"
                            ),
                            dict(id=conversation),
                        )
                    ).one()
                    assert tuple(identity) == (("restore_pending", 3) if race else ("archived", 2))
                    # The conflicting owner remains untouched, including cross-tenant storage keys.
                    row = (
                        await db.execute(
                            text(f"SELECT to_jsonb(r) FROM {table} r WHERE id=:id AND org_id=:o"),
                            dict(id=duplicate, o=conflict_org),
                        )
                    ).scalar_one()
                    assert row["name" if kind == "kb_name" else "filename"] == (
                        "hidden" if kind == "kb_name" else "duplicate.txt"
                    )
                    assert (
                        await db.scalar(
                            text("SELECT count(*) FROM conversations WHERE id=:id"),
                            dict(id=conversation),
                        )
                        == 0
                    )
        finally:
            service._dependencies = original_dependencies
            async with sessions() as db:
                await db.execute(
                    text(f"DELETE FROM {table} WHERE id=:id AND org_id=:o"),
                    dict(id=duplicate, o=conflict_org),
                )
                if conflict_org != org:
                    await db.execute(
                        text("DELETE FROM organizations WHERE id=:o"), dict(o=conflict_org)
                    )
                await db.commit()
    await service.export(org, result["archive_id"], tmp_path / "complete-export")
    assert (tmp_path / "complete-export" / "rag" / "manifest.json").exists()
    foreign = uuid4()
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO organizations(id,name,slug) VALUES(:id,'archive foreign collision',:slug)"
            ),
            dict(id=foreign, slug=str(foreign)),
        )
        await db.execute(
            text(
                "INSERT INTO file_metadata(id,org_id,storage_key,filename,size_bytes) VALUES(:id,:o,'foreign-source','foreign.txt',1)"
            ),
            dict(id=file, o=foreign),
        )
        await db.commit()
    try:
        from src.services.conversation_lifecycle import ArchiveBlocked

        with pytest.raises(ArchiveBlocked, match="restore_payload_collision"):
            await service.restore(org, result["archive_id"], apply=False)
        assert calls == [("claim", 8), ("export", 8), ("purge", 8)]
    finally:
        async with sessions() as db:
            await db.execute(
                text("DELETE FROM file_metadata WHERE id=:id AND org_id=:o"),
                dict(id=file, o=foreign),
            )
            await db.execute(text("DELETE FROM organizations WHERE id=:o"), dict(o=foreign))
            await db.commit()
    await service.restore(org, result["archive_id"], apply=True)
    assert calls == [("claim", 8), ("export", 8), ("purge", 8), ("restore", 9), ("publish", 9)]
    async with sessions() as db:
        assert (
            await db.scalar(text("SELECT storage_key FROM file_metadata WHERE id=:f"), args)
            == source
        )
        assert (
            await db.scalar(text("SELECT document_id FROM file_metadata WHERE id=:f"), args)
            == document
        )
        assert (
            await db.scalar(
                text("SELECT owning_conversation_id FROM knowledge_bases WHERE id=:k"), args
            )
            == conversation
        )
        assert (
            await db.scalars(text("SELECT to_jsonb(g) FROM access_grants g WHERE org_id=:o"), args)
        ).all() == grants
