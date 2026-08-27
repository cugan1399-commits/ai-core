"""seller session telegram chat id bigint

Revision ID: b1e6a4c8d2f0
Revises: 9c4f7e2a1b6d
Create Date: 2026-08-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1e6a4c8d2f0'
down_revision: Union[str, None] = '9c4f7e2a1b6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Telegram chat_id/user_id для части современных аккаунтов уже превышает
    # диапазон int32 (2 147 483 647) — например, реальный кейс из логов:
    # 8658993738. Меняем тип на BigInteger, чтобы такие ID помещались.
    op.alter_column(
        'seller_sessions',
        'telegram_chat_id',
        type_=sa.BigInteger(),
        existing_type=sa.Integer(),
    )


def downgrade() -> None:
    op.alter_column(
        'seller_sessions',
        'telegram_chat_id',
        type_=sa.Integer(),
        existing_type=sa.BigInteger(),
    )
