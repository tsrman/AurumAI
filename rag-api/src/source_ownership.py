"""Canonical source ownership; opaque legacy/shared keys are not purgeable."""

import re
from uuid import UUID


def owns_source(key: str, org_id: UUID, knowledge_base_id: UUID) -> bool:
    return (
        re.fullmatch(
            rf"documents/{org_id}/{knowledge_base_id}/[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}(?:\.[a-zA-Z0-9]{{1,16}})?",
            key,
        )
        is not None
    )
