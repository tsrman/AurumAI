from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


MAX_RESOURCE_ID_LENGTH = 255


class ResourceType(StrEnum):
    MODEL = "model"
    SKILL = "skill"
    KNOWLEDGE_BASE = "knowledge_base"
    AGENT_DEFINITION = "agent_definition"
    WORKFLOW = "workflow"


class PrincipalType(StrEnum):
    ORGANIZATION = "organization"
    GROUP = "group"
    USER = "user"


class AccessPermission(StrEnum):
    READ = "read"
    WRITE = "write"

    def satisfies(self, required: "AccessPermission") -> bool:
        return self is required or self is AccessPermission.WRITE


class GrantSource(StrEnum):
    ADMIN = "admin"
    MIGRATION = "migration"
    PROVISIONING = "provisioning"


@dataclass(frozen=True, slots=True)
class AuthorizationSubject:
    user_id: UUID
    org_id: UUID
    role: str = "member"

    @property
    def is_org_admin(self) -> bool:
        return self.role == "org_admin"


@dataclass(frozen=True, slots=True)
class ResourceRef:
    resource_type: ResourceType
    resource_id: str

    def __post_init__(self) -> None:
        value = self.resource_id
        if (
            not value
            or value != value.strip()
            or len(value) > MAX_RESOURCE_ID_LENGTH
            or not value.isprintable()
        ):
            raise ValueError(
                "resource_id must be nonempty, canonical, printable, and at most "
                f"{MAX_RESOURCE_ID_LENGTH} characters"
            )
