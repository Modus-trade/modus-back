"""Reviewed instructions and explicit self-service outcomes."""
from alembic import op
from app.models import KnowledgeArticle, KnowledgeAttempt, KnowledgeExposure

revision = "008"
down_revision = "007"


def upgrade():
    for model in (KnowledgeArticle, KnowledgeAttempt, KnowledgeExposure):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    for model in (KnowledgeExposure, KnowledgeAttempt, KnowledgeArticle):
        model.__table__.drop(op.get_bind())
