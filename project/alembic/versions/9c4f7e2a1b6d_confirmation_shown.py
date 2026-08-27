"""seller session confirmation shown

Revision ID: 9c4f7e2a1b6d
Revises: 5a8e2d1f9c3b
Create Date: 2026-08-27 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c4f7e2a1b6d'
down_revision: Union[str, None] = '5a8e2d1f9c3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'seller_sessions',
        sa.Column('confirmation_shown', sa.Boolean(), nullable=False, server_default='false'),
    )
    # server_default только для проставления значения существующим строкам —
    # убираем сразу после, чтобы не плодить сюрпризов на будущих INSERT.
    op.alter_column('seller_sessions', 'confirmation_shown', server_default=None)


def downgrade() -> None:
    op.drop_column('seller_sessions', 'confirmation_shown')
