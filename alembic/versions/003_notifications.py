"""In-app notifications."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"


def upgrade():
    # 001 создаёт таблицы по текущим моделям, поэтому на новой базе таблица уже может быть.
    if "notifications" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("ticket_id", sa.Integer, sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("read_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_ticket_id", "notifications", ["ticket_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade():
    op.drop_table("notifications")
