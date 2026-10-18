from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from src.core.authorization_types import AccessPermission, PrincipalType, ResourceType
from src.core.capability_catalog import Capability


class AuthorizationSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class CapabilityResponse(AuthorizationSchema):
    key: str
    label: str
    description: str


class AuthorizationPrincipalResponse(AuthorizationSchema):
    principal_type: PrincipalType
    principal_id: UUID
    label: str


class GrantSpecRequest(AuthorizationSchema):
    principal_type: PrincipalType
    principal_id: UUID
    permission: AccessPermission


class ResourceRequest(AuthorizationSchema):
    resource_type: ResourceType
    resource_id: str = Field(min_length=1, max_length=255)

    @field_validator("resource_id")
    @classmethod
    def canonical_resource_id(cls, value: str) -> str:
        if value != value.strip() or not value.isprintable():
            raise ValueError("resourceId must be canonical and printable")
        return value


class GrantCreateRequest(ResourceRequest, GrantSpecRequest):
    pass


class GrantReconcileRequest(ResourceRequest):
    # Same bound as AccessPreviewRequest.resources: every spec costs one
    # _validate_principal round trip inside the mutation's transaction, so an
    # unbounded list is a slow write holding grant rows locked.
    grants: list[GrantSpecRequest] = Field(max_length=500)


class GrantResponse(AuthorizationSchema):
    id: UUID
    resource_type: ResourceType
    resource_id: str
    principal_type: PrincipalType
    principal_id: UUID
    permission: AccessPermission
    created_by: UUID | None
    created_via: str


class CapabilityAssignmentRequest(AuthorizationSchema):
    capabilities: list[Capability]


class CapabilityAssignmentResponse(AuthorizationSchema):
    capabilities: list[Capability]


class PreviewResourceRequest(ResourceRequest):
    pass


class AccessPreviewRequest(AuthorizationSchema):
    resources: list[PreviewResourceRequest] = Field(max_length=500)


class PreviewResourceResponse(ResourceRequest):
    can_read: bool
    can_write: bool
    source: str


class AccessPreviewResponse(AuthorizationSchema):
    subject_type: PrincipalType
    subject_id: UUID
    admin_bypass: bool
    capabilities: list[Capability]
    resources: list[PreviewResourceResponse]
