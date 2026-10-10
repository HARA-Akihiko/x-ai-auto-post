"""chatgpt token sharing identity

Revision ID: d7ae8e11def9
Revises: c3c067ee2e54
Create Date: 2026-10-11 01:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7ae8e11def9'
down_revision: Union[str, Sequence[str], None] = 'c3c067ee2e54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Existing rows keep NULL identity and are treated as requiring re-authorization.
    op.add_column('chatgpt_credentials', sa.Column('subject', sa.String(length=255), nullable=True))
    op.add_column('chatgpt_credentials', sa.Column('email', sa.String(length=320), nullable=True))
    op.add_column('chatgpt_credentials', sa.Column('issuer', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chatgpt_credentials', 'issuer')
    op.drop_column('chatgpt_credentials', 'email')
    op.drop_column('chatgpt_credentials', 'subject')
