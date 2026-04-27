"""Create web_chat_sessions, web_chat_messages, plans tables and add web_password_hash to users.

Revision ID: 006
Revises: 005
Create Date: 2026-04-15

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add web_password_hash column to users table
    op.add_column('users', sa.Column('web_password_hash', sa.String(255), nullable=True))

    # Create web_chat_sessions table
    op.create_table(
        'web_chat_sessions',
        sa.Column('id', sa.Integer, nullable=False),
        sa.Column('user_id', sa.Integer, nullable=False),
        sa.Column('project_id', sa.Integer, nullable=True),
        sa.Column('title', sa.String(255), server_default='New Chat', nullable=False),
        sa.Column('mode', sa.String(20), server_default='quick', nullable=False),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column('last_message_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_web_chat_sessions_user_id', 'web_chat_sessions', ['user_id'])
    op.create_index('ix_web_chat_sessions_project_id', 'web_chat_sessions', ['project_id'])

    # Create web_chat_messages table
    op.create_table(
        'web_chat_messages',
        sa.Column('id', sa.Integer, nullable=False),
        sa.Column('session_id', sa.Integer, nullable=False),
        sa.Column('user_id', sa.Integer, nullable=False),
        sa.Column('role', sa.String(20), nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('is_complete', sa.Boolean, server_default=sa.literal(False), nullable=False),
        sa.Column('plan_file_path', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['web_chat_sessions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_web_chat_messages_session_id', 'web_chat_messages', ['session_id'])
    op.create_index('ix_web_chat_messages_user_id', 'web_chat_messages', ['user_id'])

    # Create plans table
    op.create_table(
        'plans',
        sa.Column('id', sa.Integer, nullable=False),
        sa.Column('session_id', sa.Integer, nullable=False),
        sa.Column('user_id', sa.Integer, nullable=False),
        sa.Column('project_id', sa.Integer, nullable=True),
        sa.Column('message_id', sa.Integer, nullable=False),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('status', sa.String(50), server_default='pending_review', nullable=False),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['web_chat_sessions.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['message_id'], ['web_chat_messages.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_plans_session_id', 'plans', ['session_id'])
    op.create_index('ix_plans_user_id', 'plans', ['user_id'])
    op.create_index('ix_plans_message_id', 'plans', ['message_id'])


def downgrade() -> None:
    # Drop tables in reverse order
    op.drop_index('ix_plans_message_id', table_name='plans')
    op.drop_index('ix_plans_user_id', table_name='plans')
    op.drop_index('ix_plans_session_id', table_name='plans')
    op.drop_table('plans')

    op.drop_index('ix_web_chat_messages_user_id', table_name='web_chat_messages')
    op.drop_index('ix_web_chat_messages_session_id', table_name='web_chat_messages')
    op.drop_table('web_chat_messages')

    op.drop_index('ix_web_chat_sessions_project_id', table_name='web_chat_sessions')
    op.drop_index('ix_web_chat_sessions_user_id', table_name='web_chat_sessions')
    op.drop_table('web_chat_sessions')

    # Remove column from users table
    op.drop_column('users', 'web_password_hash')
