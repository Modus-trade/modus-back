"""Resolution deadlines, reasons for changes, and durable notification deduplication."""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"


def upgrade():
    # Migration 001 creates current metadata on new installations.
    inspector = sa.inspect(op.get_bind())
    columns = {table: {column["name"] for column in inspector.get_columns(table)}
               for table in ("tickets", "ticket_history", "notifications")}
    if "resolution_due_at" not in columns["tickets"]:
        op.add_column("tickets", sa.Column("resolution_due_at", sa.DateTime(), nullable=True))
    if "resolution_due_version" not in columns["tickets"]:
        op.add_column("tickets", sa.Column("resolution_due_version", sa.Integer(), nullable=False, server_default="0"))
    if "reason" not in columns["ticket_history"]:
        op.add_column("ticket_history", sa.Column("reason", sa.Text(), nullable=True))
    if "deduplication_key" not in columns["notifications"]:
        op.add_column("notifications", sa.Column("deduplication_key", sa.String(200), nullable=True))
    if "uq_notification_deduplication_key" not in {index["name"] for index in inspector.get_indexes("notifications")}:
        op.create_index("uq_notification_deduplication_key", "notifications", ["deduplication_key"], unique=True)


def downgrade():
    op.drop_index("uq_notification_deduplication_key", table_name="notifications")
    op.drop_column("notifications", "deduplication_key")
    op.drop_column("ticket_history", "reason")
    op.drop_column("tickets", "resolution_due_version")
    op.drop_column("tickets", "resolution_due_at")
