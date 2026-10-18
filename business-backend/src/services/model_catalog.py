"""The gateway model catalog, in its fail-closed form.

`routers/models.py` has its own reader that falls back to a mock list when the
gateway is unreachable, so the picker keeps working in DEBUG. Authorization must
not do that: inventing model ids would mean granting or validating access to
models that may not exist. Everything on the authorization path — grant
validation (`resource_registry`) and organization provisioning — reads the
catalog through here, where an outage yields an empty list and therefore no
access at all.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from src.core.config import settings
from src.core.logging import logger


ModelCatalog = Callable[[], Awaitable[list[dict[str, object]]]]


class GatewayCatalogStatus(StrEnum):
    AVAILABLE = "available"
    MOCK = "mock"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class GatewayCatalogSnapshot:
    status: GatewayCatalogStatus
    models: tuple[dict[str, object], ...]


# Substrings that mark a model as something other than a chat model. The
# gateway's /v1/models is the OpenAI shape — `id`, `created`, `owned_by` and
# nothing that states a modality — so the id is genuinely all there is to go
# on; there is no metadata field being ignored here.
#
# Each entry names a model family that rejects /v1/chat/completions:
# embeddings, realtime speech, transcription, text-to-speech and rerankers.
# `deprecated` and `rc` model variants still serve chat completions and are
# deliberately NOT filtered — "not the newest" is not "not a chat model".
#
# Keep this list narrow and evidence-based. Over-filtering silently hides a
# working model from every user in the org, which is harder to notice than
# the reverse: an unusable model that slips through now produces a real,
# visible gateway error (see services/llm_client.py) rather than the blank
# reply this list was introduced to prevent.
_NON_CHAT_MODEL_MARKERS = (
    "text-embedding",  # OpenAI-style embedding ids
    "speech-realtime",  # realtime speech models: a different API, not chat
    "/rerank",
    "whisper",
    "tts-",
)


def is_chat_model(model_id: str) -> bool:
    """Whether `model_id` can serve a chat completion.

    Embedding and speech models are listed by the gateway alongside chat
    models but reject /v1/chat/completions. They used to be offered in the
    UI's model picker, and — because the picker defaults to the first entry
    and an embedding model could sort first — it became the *default*
    for anyone who had never picked one by hand, so their very first message
    got an empty reply.
    """
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _NON_CHAT_MODEL_MARKERS)


async def gateway_catalog_snapshot(
    *, timeout: float = 30.0
) -> GatewayCatalogSnapshot:
    """Return the catalog together with its trustworthy source state."""
    if settings.DEBUG and not settings.GATEWAY_API_KEY:
        return GatewayCatalogSnapshot(
            GatewayCatalogStatus.MOCK,
            (
                {"id": "gpt-4o-mini"},
                {"id": "gpt-4o"},
                {"id": "claude-3-5-sonnet"},
            ),
        )
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=settings.gateway_verify) as client:
            response = await client.get(
                f"{settings.LLM_GATEWAY_URL}/v1/models",
                headers={
                    "Authorization": f"Bearer {settings.GATEWAY_API_KEY}",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload: Any = response.json()
    except Exception:
        logger.warning("authorization_resource_catalog_unavailable", exc_info=True)
        return GatewayCatalogSnapshot(GatewayCatalogStatus.UNAVAILABLE, ())
    if isinstance(payload, dict):
        if "data" not in payload:
            logger.warning("authorization_resource_catalog_invalid_payload")
            return GatewayCatalogSnapshot(GatewayCatalogStatus.UNAVAILABLE, ())
        models: Any = payload["data"]
    else:
        models = payload
    if not isinstance(models, list):
        logger.warning("authorization_resource_catalog_invalid_payload")
        return GatewayCatalogSnapshot(GatewayCatalogStatus.UNAVAILABLE, ())
    return GatewayCatalogSnapshot(
        GatewayCatalogStatus.AVAILABLE,
        tuple(model for model in models if isinstance(model, dict)),
    )


async def gateway_models(*, timeout: float = 30.0) -> list[dict[str, object]]:
    """Every model the gateway reports, or `[]` if it cannot be reached."""
    return list((await gateway_catalog_snapshot(timeout=timeout)).models)


async def chat_model_ids(catalog: ModelCatalog = gateway_models) -> tuple[str, ...]:
    """Ids of every chat-capable model the gateway currently reports.

    Order follows the gateway so a seeded default is reproducible; duplicates
    are dropped because a grant is unique per (resource, principal, permission)
    anyway.
    """
    seen: dict[str, None] = {}
    for model in await catalog():
        model_id = model.get("id")
        if isinstance(model_id, str) and model_id and is_chat_model(model_id):
            seen.setdefault(model_id, None)
    return tuple(seen)


# Organization provisioning runs inside the request that creates the
# organization — the user's very first login. Waiting the full 30 seconds on an
# unresponsive gateway there would turn a slow gateway into a slow login; the
# cost of giving up early is one re-run of `python -m src.cli.provision_org`.
PROVISIONING_CATALOG_TIMEOUT_SECONDS = 5.0


async def provisioning_models() -> list[dict[str, object]]:
    return await gateway_models(timeout=PROVISIONING_CATALOG_TIMEOUT_SECONDS)
