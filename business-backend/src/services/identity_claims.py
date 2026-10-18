"""Shared interpretation of group claims from an already validated identity token."""

from collections.abc import Mapping

from fastapi import HTTPException

SYNC_EXCLUDED_GROUP_PREFIXES = ("org-",)
GROUPS_COMPLETE_CLAIM = "platform_groups_complete"


def normalize_groups(raw_claims: Mapping[str, object]) -> set[str] | None:
    """Return authoritative normalized groups, or None when membership is unknown.

    Keycloak omits its group claim for zero memberships. The operator-owned,
    signed boolean marker certifies that the group mapper is present, making
    that omission authoritative. Without that exact marker, absence retains
    the legacy preserve-membership meaning. An explicit claim always wins.
    """
    if "groups" in raw_claims:
        groups = raw_claims["groups"]
    elif raw_claims.get(GROUPS_COMPLETE_CLAIM) is True:
        groups = []
    else:
        return None
    if groups is None:
        groups = []
    if not isinstance(groups, list) or len(groups) > 256:
        raise HTTPException(400, detail="Invalid identity groups")
    names: set[str] = set()
    for name in groups:
        if not isinstance(name, str) or len(name) > 255:
            raise HTTPException(400, detail="Invalid identity groups")
        name = name.lstrip("/")
        if name and not name.startswith(SYNC_EXCLUDED_GROUP_PREFIXES):
            names.add(name)
    return names
