"""Author confirmation and immutable reviews of individual resolution revisions."""
from alembic import op
import sqlalchemy as sa
from app.models import ResolutionReview

revision = "006"
down_revision = "005"


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("tickets")}
    if "resolution_review_version" not in columns:
        op.add_column("tickets", sa.Column("resolution_review_version", sa.Integer(), nullable=False, server_default="0"))
    if "resolution_confirmed_at" not in columns:
        op.add_column("tickets", sa.Column("resolution_confirmed_at", sa.DateTime(), nullable=True))
    if not inspector.has_table("resolution_reviews"):
        ResolutionReview.__table__.create(op.get_bind())


def downgrade():
    op.drop_table("resolution_reviews")
    op.drop_column("tickets", "resolution_confirmed_at")
    op.drop_column("tickets", "resolution_review_version")
