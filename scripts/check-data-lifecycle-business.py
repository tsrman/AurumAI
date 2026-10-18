"""Independent exact business payload/identity check for the disposable fixture."""

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from uuid import UUID

import asyncpg

STATE = Path('/tmp/phaseb-lifecycle-acceptance/archive-fixture.json')
EXPECTED = {'conversations': 1, 'messages': 52, 'file_metadata': 1, 'skills': 1, 'knowledge_bases': 1}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def payload_rows(table, rows, knowledge_base_id):
    result = [dict(row) for row in rows]
    if table == 'skills':
        # This fixture is created by conversation_attachments with this exact
        # schema/order. Normalize its JSON wire representation, preserving the
        # original baseline hash; never replace the recorded baseline on restore.
        schema = {'type': 'object', 'properties': {
            'knowledge_base_id': {'type': 'string', 'const': knowledge_base_id},
            'query': {'type': 'string'}, 'limit': {'type': 'integer'}},
            'required': ['knowledge_base_id', 'query']}
        for row in result:
            if not isinstance(row['input_schema'], str) or json.loads(row['input_schema']) != schema:
                raise ValueError('original_fixture_skill_schema_required')
            row['input_schema'] = json.dumps(schema)
    return result


def verify(stage, baseline, payload, retained, lifecycle, generation, restored, archive_id=None):
    if retained != baseline['retained']:
        raise ValueError('retained_identity_or_grant_changed')
    if stage == 'archived':
        if not archive_id or lifecycle != 'archived' or any(item['count'] for item in payload.values()):
            raise ValueError('business_payload_not_purged')
        if generation <= baseline['generation']:
            raise ValueError('archive_generation_not_advanced')
        if 'archive_generation' in baseline and (generation != baseline['archive_generation'] or archive_id != baseline['archive_id']):
            raise ValueError('established_archive_changed')
    else:
        if lifecycle != 'active' or payload != baseline['payload']:
            raise ValueError('business_payload_not_preserved')
        if stage == 'restored' and (not restored or 'archive_generation' not in baseline
                                     or generation <= baseline['archive_generation']
                                     or archive_id != baseline.get('archive_id')):
            raise ValueError('restore_generation_required')
        if stage == 'before' and (restored or generation != baseline['generation']):
            raise ValueError('original_generation_required')


def save(state):
    temporary = STATE.with_suffix('.business-tmp')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE)
    directory = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def check(stage):
    spec = importlib.util.spec_from_file_location('history_fixture', Path(__file__).with_name('prepare-data-lifecycle-fixture.py'))
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)
    state = json.loads(STATE.read_text())
    if history.owned_postgres() != state['postgres_container_id']:
        raise ValueError('established_database_mismatch')
    org, conversation = UUID(state['org_id']), UUID(state['conversation_id'])
    attachment = state['attachment']
    resources = [str(UUID(attachment[key])) for key in ('knowledge_base_id', 'skill_id')]
    connection = await asyncpg.connect('postgresql://lifecycle_data_operator:lifecycle-data-operator-password@127.0.0.1:55434/aurum_ai', timeout=10)
    try:
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            await connection.execute("SELECT set_config('app.current_org_id',$1,true)", str(org))
            payload = {}
            for table, expected in EXPECTED.items():
                owner = 'id' if table == 'conversations' else ('conversation_id' if table == 'messages' else 'owning_conversation_id')
                where = f'org_id=$1 AND {owner}=$2'
                bound = await connection.fetchrow(f'SELECT count(*) AS n,coalesce(max(octet_length(row_to_json(t)::text)),0) AS bytes FROM {table} t WHERE {where}', org, conversation)
                if bound['n'] > expected or bound['bytes'] > 1024 * 1024:
                    raise ValueError('bounded_fixture_required')
                rows = await connection.fetch(f'SELECT * FROM {table} WHERE {where} ORDER BY id', org, conversation)
                payload[table] = {'count': len(rows), 'sha256': digest(payload_rows(table, rows, attachment['knowledge_base_id']))}
            identity = await connection.fetchrow('SELECT * FROM conversation_identities WHERE org_id=$1 AND id=$2', org, conversation)
            if identity is None:
                raise ValueError('conversation_identity_missing')
            messages = await connection.fetch('SELECT id,org_id,conversation_id,created_at,turn_id,role FROM message_identities WHERE org_id=$1 AND conversation_id=$2 ORDER BY id LIMIT 53', org, conversation)
            expected_ids = {UUID(turn[role + '_message']) for turn in state['turns'] for role in ('user', 'assistant')}
            if len(messages) != 52 or {row['id'] for row in messages} != expected_ids:
                raise ValueError('message_identities_missing')
            grants = await connection.fetch('SELECT * FROM access_grants WHERE org_id=$1 AND resource_id=ANY($2::text[]) ORDER BY id LIMIT 33', org, resources)
            if len(grants) != 2:
                raise ValueError('original_grants_required')
            executions = await connection.fetch('SELECT namespace,workflow_id,run_id,workflow_type,task_queue,parent_workflow_id,parent_run_id,terminal_status,tree_complete FROM conversation_executions WHERE org_id=$1 AND conversation_id=$2 ORDER BY run_id LIMIT 2', org, conversation)
            if (len(executions) != 1 or str(executions[0]['run_id']) != state['run_id']
                    or (executions[0]['terminal_status'] or '').upper() != 'COMPLETED'
                    or not executions[0]['tree_complete']):
                raise ValueError('original_execution_proof_required')
            retained = {'conversation_identity': digest({key: identity[key] for key in ('id','org_id','user_id','created_at')}),
                        'message_identities': digest([dict(row) for row in messages]),
                        'grants': digest([dict(row) for row in grants]),
                        'execution': digest([dict(row) for row in executions])}
    finally:
        await connection.close()
    baseline = state.get('business_baseline')
    if baseline is None:
        if (stage != 'before' or identity['lifecycle'] != 'active' or identity['archive_id'] is not None
                or identity['restored_at'] is not None or any(payload[key]['count'] != count for key, count in EXPECTED.items())):
            raise ValueError('original_fixture_required')
        baseline = {'payload': payload, 'retained': retained, 'generation': identity['generation']}
        state['business_baseline'] = baseline
        save(state)
    archive_id = str(identity['archive_id']) if identity['archive_id'] is not None else None
    verify(stage, baseline, payload, retained, identity['lifecycle'], identity['generation'], identity['restored_at'] is not None, archive_id)
    if stage == 'archived' and 'archive_generation' not in baseline:
        baseline.update(archive_generation=identity['generation'], archive_id=archive_id)
        save(state)
    print('Owned business payload/identities/grants/exact execution verified:', stage)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ('before', 'archived', 'restored'):
            raise ValueError('explicit_stage_required')
        asyncio.run(check(sys.argv[1]))
    except Exception:
        raise SystemExit('Owned business verification blocked; preserve private fixture journal') from None
