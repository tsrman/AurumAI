"""Pictures the user uploads for Hermes Agent's workspace.

Revision ID: 20261104_upload_images
Revises: 20261103_file_uploads

An upload may be a PNG, JPEG or WebP picture, which Hermes works on in its
terminal. Pictures an agent attaches stay conversation images.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261104_upload_images"
down_revision: str | None = "20261103_file_uploads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "conversation_files"
FILE_TYPES = (
    "'text/csv','text/plain','text/markdown','application/json','application/pdf',"
    "'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',"
    "'application/vnd.openxmlformats-officedocument.wordprocessingml.document',"
    "'application/vnd.openxmlformats-officedocument.presentationml.presentation',"
    "'application/zip'"
)
IMAGE_TYPES = "'image/png','image/jpeg','image/webp'"


def _content_types(condition: str) -> None:
    op.drop_constraint("ck_conversation_file_content_type", TABLE, type_="check")
    op.create_check_constraint("ck_conversation_file_content_type", TABLE, condition)


def upgrade() -> None:
    _content_types(
        f"content_type IN ({FILE_TYPES}) OR (origin = 'uploaded' AND content_type IN ({IMAGE_TYPES}))"
    )


def downgrade() -> None:
    # Fails instead of deleting uploaded pictures while any exist.
    _content_types(f"content_type IN ({FILE_TYPES})")
