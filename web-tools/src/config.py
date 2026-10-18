"""Environment-only operator configuration for the isolated web service."""

from ipaddress import ip_network

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def split_forbidden_cidrs(value: str) -> tuple[str, ...]:
    """Parse the same comma-separated form the egress guard's env var uses."""
    raw = value.strip()
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(","))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEB_TOOLS_", extra="forbid")

    # The invalid default keeps type-checkers aware the env-backed constructor
    # takes no Python argument while still failing validation at runtime.
    SERVICE_SECRET: SecretStr = Field(default=SecretStr(""), min_length=16)
    HOST: str = "0.0.0.0"
    PORT: int = Field(default=8120, ge=1, le=65535)
    SEARXNG_URL: AnyHttpUrl = AnyHttpUrl("http://searxng:8080")
    # Operator-supplied deny-list, same comma-separated value the egress guard
    # reads as WEB_EGRESS_FORBIDDEN_CIDRS. In Kubernetes there is no nftables
    # sidecar, so this is the only place an operator range is enforced.
    FORBIDDEN_CIDRS: str = ""

    @field_validator("FORBIDDEN_CIDRS")
    @classmethod
    def canonical_forbidden_cidrs(cls, value: str) -> str:
        for candidate in split_forbidden_cidrs(value):
            if not candidate:
                raise ValueError("WEB_TOOLS_FORBIDDEN_CIDRS contains an empty item")
            try:
                network = ip_network(candidate, strict=True)
            except ValueError as exc:
                raise ValueError(
                    "WEB_TOOLS_FORBIDDEN_CIDRS must contain canonical IPv4/IPv6 CIDRs"
                ) from exc
            if str(network) != candidate:
                raise ValueError(
                    "WEB_TOOLS_FORBIDDEN_CIDRS must contain canonical IPv4/IPv6 CIDRs"
                )
        return value

    @property
    def forbidden_cidrs(self) -> tuple[str, ...]:
        return split_forbidden_cidrs(self.FORBIDDEN_CIDRS)

    @field_validator("SEARXNG_URL")
    @classmethod
    def internal_search_origin_only(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if (
            value.username
            or value.password
            or value.query
            or value.fragment
            or value.path not in {"", "/"}
        ):
            raise ValueError("WEB_TOOLS_SEARXNG_URL must be an origin without credentials")
        return value
