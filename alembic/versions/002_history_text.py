"""Store complete resolution revisions in ticket history."""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"


def upgrade():
    with op.batch_alter_table("ticket_history") as batch:
        batch.alter_column("old_value", existing_type=sa.String(255), type_=sa.Text())
        batch.alter_column("new_value", existing_type=sa.String(255), type_=sa.Text())


def downgrade():
    with op.batch_alter_table("ticket_history") as batch:
        batch.alter_column("old_value", existing_type=sa.Text(), type_=sa.String(255))
        batch.alter_column("new_value", existing_type=sa.Text(), type_=sa.String(255))
