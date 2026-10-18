import uuid

from sqlalchemy import inspect

from src.models.knowledge_base import KnowledgeBase
from src.schemas.knowledge_base import KnowledgeBaseCreate, KnowledgeBaseResponse


def test_knowledge_base_is_a_canonical_org_scoped_resource() -> None:
    mapper = inspect(KnowledgeBase)
    columns = mapper.columns

    assert set(columns.keys()) == {
        "id",
        "org_id",
        "created_by",
        "name",
        "description",
        "owning_conversation_id",
        "created_at",
        "updated_at",
    }
    assert columns["org_id"].nullable is False
    assert columns["created_by"].nullable is True
    assert columns["name"].nullable is False
    assert columns["name"].type.length == 255
    assert columns["owning_conversation_id"].nullable is True
    assert columns["owning_conversation_id"].unique is True
    assert {fk.target_fullname for fk in columns["org_id"].foreign_keys} == {
        "organizations.id"
    }
    assert {fk.target_fullname for fk in columns["created_by"].foreign_keys} == {
        "users.id"
    }
    assert {
        fk.target_fullname for fk in columns["owning_conversation_id"].foreign_keys
    } == {"conversation_identities.id"}


def test_knowledge_base_api_contract_has_stable_uuid_and_trimmed_fields() -> None:
    payload = KnowledgeBaseCreate(name="  Regulations  ", description="  Internal rules  ")
    resource_id = uuid.uuid4()

    response = KnowledgeBaseResponse(
        id=resource_id,
        name=payload.name,
        description=payload.description,
        canWrite=True,
        createdAt="2026-09-01T00:00:00Z",
        updatedAt="2026-09-01T00:00:00Z",
    )

    assert payload.name == "Regulations"
    assert payload.description == "Internal rules"
    assert response.id == resource_id
    assert response.canWrite is True


def test_knowledge_base_name_rejects_blank_values() -> None:
    try:
        KnowledgeBaseCreate(name="   ", description="ignored")
        assert False, "expected blank knowledge-base name to fail"
    except ValueError:
        pass
