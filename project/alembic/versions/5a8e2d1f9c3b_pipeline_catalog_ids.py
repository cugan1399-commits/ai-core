"""seller pipeline catalog ids

Revision ID: 5a8e2d1f9c3b
Revises: 3f1c9a2b7d4e
Create Date: 2026-08-27 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5a8e2d1f9c3b'
down_revision: Union[str, None] = '3f1c9a2b7d4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'seller_pipelines',
        sa.Column('bitrix_catalog_ids', sa.JSON(), nullable=False, server_default='[]'),
    )
    # server_default только для проставления значения существующим строкам при
    # миграции — в самой модели дефолт на уровне Python (default=list), убираем
    # server_default сразу после, чтобы не плодить сюрпризов на будущих INSERT.
    op.alter_column('seller_pipelines', 'bitrix_catalog_ids', server_default=None)


def downgrade() -> None:
    op.drop_column('seller_pipelines', 'bitrix_catalog_ids')