PLATFORM_DEFAULT_TEMPERATURE = 0.7


def resolve_temperature(override: float | None, org_default: float | None) -> float:
    """Cascades an explicit override over the organization default over the
    platform-wide fallback (preserves the pre-existing hardcoded 0.7 default
    for organizations/agents/conversations that configure nothing)."""
    if override is not None:
        return override
    if org_default is not None:
        return org_default
    return PLATFORM_DEFAULT_TEMPERATURE
