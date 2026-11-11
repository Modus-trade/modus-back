"""Opt-in shared problems and unique affected employees."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"


def upgrade():
    bind = op.get_bind()
    # The initial migration creates current metadata on a fresh installation.
    inspector = sa.inspect(bind)
    if "is_shared" not in {c["name"] for c in inspector.get_columns("tickets")}:
        op.add_column("tickets", sa.Column("is_shared", sa.Boolean(), nullable=False, server_default=sa.false()))
    if not inspector.has_table("ticket_participants"):
        op.create_table("ticket_participants",
            sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id"), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False))
        op.create_index("ix_ticket_participants_user_id", "ticket_participants", ["user_id"])


def downgrade():
    op.drop_table("ticket_participants")
    with op.batch_alter_table("tickets") as batch:
        batch.drop_column("is_shared")
