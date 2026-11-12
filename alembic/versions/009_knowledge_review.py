"""Drafts, explicit publication approval, private revisions and reports."""
from alembic import op
import sqlalchemy as sa
from app.models import KnowledgeAudit, KnowledgeDraft, KnowledgeReport, KnowledgeRevision

revision = "009"
down_revision = "008"


def upgrade():
    bind = op.get_bind()
    for model in (KnowledgeDraft, KnowledgeRevision, KnowledgeAudit, KnowledgeReport):
        model.__table__.create(bind, checkfirst=True)
    # Retain existing text for review, while withdrawing legacy publications.
    bind.execute(sa.text("""
        INSERT INTO knowledge_drafts
            (article_id,title,summary,body,visibility,owner_department,status,version,review_days,editor_id,updated_at)
        SELECT a.id,a.title,a.summary,a.body,a.visibility,u.department,'DRAFT',1,90,a.editor_id,a.updated_at
        FROM knowledge_articles a
        JOIN tickets t ON t.id=a.source_ticket_id JOIN users u ON u.id=t.author_id
        WHERE NOT EXISTS (SELECT 1 FROM knowledge_drafts d WHERE d.article_id=a.id)
    """))
    bind.execute(sa.text("""
        UPDATE knowledge_articles SET is_published=false
        WHERE NOT EXISTS (SELECT 1 FROM knowledge_revisions r
                          WHERE r.article_id=knowledge_articles.id AND r.version=knowledge_articles.version)
    """))


def downgrade():
    # A rollback keeps articles withdrawn until explicitly checked again.
    op.execute("UPDATE knowledge_articles SET is_published=false")
    for model in (KnowledgeReport, KnowledgeAudit, KnowledgeRevision, KnowledgeDraft):
        model.__table__.drop(op.get_bind())
