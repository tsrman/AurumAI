from dataclasses import FrozenInstanceError

import pytest

from src.core.authorization_types import (
    AccessPermission,
    GrantSource,
    PrincipalType,
    ResourceRef,
    ResourceType,
)


def test_authorization_enums_reject_values_outside_the_closed_contract() -> None:
    assert {item.value for item in ResourceType} == {
        "model",
        "skill",
        "knowledge_base",
        "agent_definition",
        "workflow",
    }
    assert {item.value for item in PrincipalType} == {"organization", "group", "user"}
    assert {item.value for item in AccessPermission} == {"read", "write"}
    assert {item.value for item in GrantSource} == {"admin", "migration", "provisioning"}

    with pytest.raises(ValueError):
        ResourceType("document")
    with pytest.raises(ValueError):
        PrincipalType("anyone")
    with pytest.raises(ValueError):
        AccessPermission("deny")


@pytest.mark.parametrize(
    ("granted", "required", "expected"),
    [
        (AccessPermission.READ, AccessPermission.READ, True),
        (AccessPermission.READ, AccessPermission.WRITE, False),
        (AccessPermission.WRITE, AccessPermission.READ, True),
        (AccessPermission.WRITE, AccessPermission.WRITE, True),
    ],
)
def test_write_permission_implies_read_but_read_never_implies_write(
    granted: AccessPermission,
    required: AccessPermission,
    expected: bool,
) -> None:
    assert granted.satisfies(required) is expected


def test_resource_ref_preserves_valid_gateway_ids_and_is_immutable() -> None:
    resource = ResourceRef(ResourceType.MODEL, "openrouter/vendor:model/v2")

    assert resource.resource_id == "openrouter/vendor:model/v2"
    with pytest.raises(FrozenInstanceError):
        resource.resource_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "resource_id",
    ["", " skill-id", "skill-id ", "line\nbreak", "x" * 256],
)
def test_resource_ref_rejects_noncanonical_or_unbounded_ids(resource_id: str) -> None:
    with pytest.raises(ValueError, match="resource_id"):
        ResourceRef(ResourceType.SKILL, resource_id)
