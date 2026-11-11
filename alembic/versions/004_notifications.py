"""Persistent per-user notification inbox."""
from alembic import op
import sqlalchemy as sa
from app.models import Notification
revision = "004"
down_revision = "003"


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("notifications"):
        Notification.__table__.create(op.get_bind())


def downgrade():
    op.drop_table("notifications")
