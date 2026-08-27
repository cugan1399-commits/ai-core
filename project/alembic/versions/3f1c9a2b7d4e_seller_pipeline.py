"""seller pipeline

Revision ID: 3f1c9a2b7d4e
Revises: 808f53c325b5
Create Date: 2026-08-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f1c9a2b7d4e'
down_revision: Union[str, None] = '808f53c325b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- seller_pipelines: новая таблица, одно направление продаж = один Telegram-бот ---
    op.create_table(
        'seller_pipelines',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('member_id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('telegram_bot_token', sa.String(), nullable=False),
        sa.Column('bitrix_category_id', sa.Integer(), nullable=False),
        sa.Column('bitrix_line_id', sa.Integer(), nullable=False),
        sa.Column('field_schema', sa.JSON(), nullable=False),
        sa.Column('stages', sa.JSON(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['member_id'], ['clients.member_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('telegram_bot_token'),
    )

    # --- knowledge_chunks: member_id -> pipeline_id (таблица пустая, миграция данных не нужна) ---
    op.drop_index('ux_knowledge_chunks_source', table_name='knowledge_chunks')
    op.drop_constraint('knowledge_chunks_member_id_fkey', 'knowledge_chunks', type_='foreignkey')
    op.drop_column('knowledge_chunks', 'member_id')

    op.add_column('knowledge_chunks', sa.Column('pipeline_id', sa.Integer(), nullable=False))
    op.create_foreign_key(
        'knowledge_chunks_pipeline_id_fkey',
        'knowledge_chunks', 'seller_pipelines',
        ['pipeline_id'], ['id'],
    )
    op.create_index(
        'ux_knowledge_chunks_source', 'knowledge_chunks',
        ['pipeline_id', 'source_type', 'source_id'], unique=True,
    )

    # --- seller_sessions: новая таблица, состояние диалога с одним клиентом Telegram ---
    op.create_table(
        'seller_sessions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('pipeline_id', sa.Integer(), nullable=False),
        sa.Column('telegram_chat_id', sa.Integer(), nullable=False),
        sa.Column('bitrix_chat_id', sa.Integer(), nullable=True),
        sa.Column('bitrix_dialog_id', sa.String(), nullable=True),
        sa.Column('bitrix_deal_id', sa.Integer(), nullable=True),
        sa.Column('collected_fields', sa.JSON(), nullable=False),
        sa.Column('current_stage_key', sa.String(), nullable=False),
        sa.Column('last_bot_message_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status in ('active', 'completed', 'escalated')",
            name='ck_seller_sessions_status',
        ),
        sa.ForeignKeyConstraint(['pipeline_id'], ['seller_pipelines.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ux_seller_sessions_active_per_chat', 'seller_sessions',
        ['pipeline_id', 'telegram_chat_id'], unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        'ux_seller_sessions_active_per_chat', table_name='seller_sessions',
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table('seller_sessions')

    op.drop_index('ux_knowledge_chunks_source', table_name='knowledge_chunks')
    op.drop_constraint('knowledge_chunks_pipeline_id_fkey', 'knowledge_chunks', type_='foreignkey')
    op.drop_column('knowledge_chunks', 'pipeline_id')

    op.add_column('knowledge_chunks', sa.Column('member_id', sa.String(), nullable=False))
    op.create_foreign_key(
        'knowledge_chunks_member_id_fkey',
        'knowledge_chunks', 'clients',
        ['member_id'], ['member_id'],
    )
    op.create_index(
        'ux_knowledge_chunks_source', 'knowledge_chunks',
        ['member_id', 'source_type', 'source_id'], unique=True,
    )

    op.drop_table('seller_pipelines')