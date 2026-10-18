"""Private operator RAG wire client. Inputs contain exact identities, never secrets."""

from typing import Literal
from uuid import UUID
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Action = Literal["claim", "export", "purge", "restore", "publish", "release", "status"]
State = Literal[
    "claimed", "verified", "purging", "purged", "restore_pending", "restored", "released"
]


class RagArchiveError(RuntimeError):
    pass


class RagArchivePendingStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: Literal["vector_delete", "source_delete", "source_restore", "vector_restore"]
    resource: str = Field(max_length=128)


class RagArchiveReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archive_id: UUID
    generation: int = Field(gt=0)
    state: State
    archive_generation: int | None = Field(default=None, gt=0)
    next_restore_generation: int | None = Field(default=None, gt=0)
    pending_steps: list[RagArchivePendingStep] = Field(default_factory=list, max_length=100)
    receipt_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class RagArchiveInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["active", "archiving", "archived", "restore_pending"]
    generation: int = Field(gt=0)
    archive_id: UUID | None


class RagArchiveClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, service_secret: str):
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.service_secret = service_secret

    async def command(
        self,
        action: Action,
        *,
        org_id: UUID,
        knowledge_base_id: UUID,
        archive_id: UUID,
        generation: int,
        dry_run: bool = False,
    ) -> RagArchiveReceipt:
        if not self.service_secret or generation <= 0:
            raise RagArchiveError("rag_archive_not_configured")
        try:
            response = await self.http.post(
                f"{self.base_url}/internal/archive/{action}",
                headers={"X-Archive-Service-Secret": self.service_secret},
                json=dict(
                    org_id=str(org_id),
                    knowledge_base_id=str(knowledge_base_id),
                    archive_id=str(archive_id),
                    generation=generation,
                    dry_run=dry_run,
                ),
            )
            if response.status_code == 409:
                raise RagArchiveError("rag_archive_blocked")
            response.raise_for_status()
            receipt = RagArchiveReceipt.model_validate(response.json())
        except (ValueError, ValidationError):
            raise RagArchiveError("rag_archive_response_invalid") from None
        except httpx.HTTPError:
            raise RagArchiveError("rag_archive_unavailable") from None
        if (
            receipt.archive_id != archive_id
            or receipt.generation != generation
            or (receipt.state not in ("claimed", "released") and receipt.receipt_hash is None)
        ):
            raise RagArchiveError("rag_archive_response_invalid")
        return receipt

    async def inspect(self, *, org_id: UUID, knowledge_base_id: UUID) -> RagArchiveInspection:
        if not self.service_secret:
            raise RagArchiveError("rag_archive_not_configured")
        try:
            response = await self.http.post(
                f"{self.base_url}/internal/archive/inspect",
                headers={"X-Archive-Service-Secret": self.service_secret},
                json=dict(org_id=str(org_id), knowledge_base_id=str(knowledge_base_id)),
            )
            response.raise_for_status()
            return RagArchiveInspection.model_validate(response.json())
        except ValueError:
            raise RagArchiveError("rag_archive_response_invalid") from None
        except httpx.HTTPError:
            raise RagArchiveError("rag_archive_unavailable") from None
