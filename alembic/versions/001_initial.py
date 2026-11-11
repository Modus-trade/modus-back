"""Initial relational schema."""

from alembic import op
from app.database import Base
from app import models  # noqa: F401 — register mapped tables before migration

revision = "001"
down_revision = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())
