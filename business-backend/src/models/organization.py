from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from src.models.base import BaseModel


class Organization(BaseModel):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    tier: Mapped[str | None] = mapped_column(String(20), default="standard")
    quota_requests: Mapped[int | None] = mapped_column(Integer, default=10000)
    quota_tokens: Mapped[int | None] = mapped_column(BigInteger, default=1_000_000_000)
