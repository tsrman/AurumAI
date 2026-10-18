"""Independent acceptance must detect lost identities and altered restored payload."""

import importlib.util
import json
from pathlib import Path

import pytest


def load():
    spec = importlib.util.spec_from_file_location(
        'business_checker', Path(__file__).parents[1] / 'check-data-lifecycle-business.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    payload = {key: {'count': count, 'sha256': key} for key, count in
               [('conversations', 1), ('messages', 52), ('file_metadata', 1),
                ('skills', 1), ('knowledge_bases', 1)]}
    retained = {'conversation_identity': 'original', 'message_identities': 'original', 'grants': 'original'}
    baseline = {'payload': payload, 'retained': retained, 'generation': 1}
    return baseline, payload, retained


def test_skill_json_key_order_preserves_original_baseline_hash():
    module = load()
    schema = {'type': 'object', 'properties': {
        'knowledge_base_id': {'type': 'string', 'const': 'owned-kb'},
        'query': {'type': 'string'}, 'limit': {'type': 'integer'}},
        'required': ['knowledge_base_id', 'query']}
    original = {'input_schema': json.dumps(schema), 'description': 'original',
                'allowed_child_skills': None}
    reordered = {**original, 'input_schema': json.dumps(schema, sort_keys=True)}
    assert module.digest([reordered]) != module.digest([original])
    assert module.digest(module.payload_rows('skills', [reordered], 'owned-kb')) == module.digest([original])
    changed = {**reordered, 'description': 'changed'}
    assert module.digest(module.payload_rows('skills', [changed], 'owned-kb')) != module.digest([original])
    json_null = {**reordered, 'allowed_child_skills': 'null'}
    assert module.digest(module.payload_rows('skills', [json_null], 'owned-kb')) != module.digest([original])
    schema['properties']['knowledge_base_id']['const'] = 'foreign-kb'
    with pytest.raises(ValueError):
        module.payload_rows('skills', [{**original, 'input_schema': json.dumps(schema)}], 'owned-kb')


def test_archived_stage_requires_payload_absence_and_original_identities():
    module = load()
    baseline, payload, retained = fixture()
    empty = {key: {'count': 0, 'sha256': 'empty'} for key in payload}
    module.verify('archived', baseline, empty, retained, 'archived', 2, False, 'owned-archive')
    with pytest.raises(ValueError):
        module.verify('archived', baseline, payload, retained, 'archived', 2, False, 'owned-archive')
    with pytest.raises(ValueError):
        module.verify('archived', baseline, empty, {**retained, 'message_identities': 'lost'}, 'archived', 2, False, 'owned-archive')


def test_restore_requires_exact_payload_and_new_generation():
    module = load()
    baseline, payload, retained = fixture()
    baseline.update(archive_generation=2, archive_id='owned-archive')
    module.verify('restored', baseline, payload, retained, 'active', 3, True, 'owned-archive')
    with pytest.raises(ValueError):
        module.verify('restored', baseline, {**payload, 'file_metadata': {'count': 1, 'sha256': 'changed'}}, retained, 'active', 3, True, 'owned-archive')
    with pytest.raises(ValueError):
        module.verify('restored', baseline, payload, retained, 'active', 1, False, 'owned-archive')


@pytest.mark.parametrize('proof', ['same_generation', 'missing_archive', 'wrong_archive'])
def test_restore_requires_proven_archive_generation(proof):
    module = load()
    baseline, payload, retained = fixture()
    if proof != 'missing_archive':
        baseline.update(archive_generation=2, archive_id='owned-archive')
    with pytest.raises(ValueError):
        module.verify('restored', baseline, payload, retained, 'active', 2 if proof == 'same_generation' else 3, True, 'foreign' if proof == 'wrong_archive' else 'owned-archive')
