"""Голосовые сообщения в обсуждении: ссылка на запись, длительность, расшифровка."""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("comments")}
    with op.batch_alter_table("comments") as batch:
        if "audio_id" not in columns:
            batch.add_column(sa.Column("audio_id", sa.Integer(), sa.ForeignKey("attachments.id", name="fk_comments_audio"), nullable=True))
        if "audio_seconds" not in columns:
            batch.add_column(sa.Column("audio_seconds", sa.Integer(), nullable=True))
        if "transcript" not in columns:
            batch.add_column(sa.Column("transcript", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("comments") as batch:
        batch.drop_column("transcript")
        batch.drop_column("audio_seconds")
        batch.drop_column("audio_id")
