"""Optional 1C item snapshot on an appeal."""
from alembic import op
import sqlalchemy as sa
revision = '012'
down_revision = '011'

def upgrade():
    if 'onec_item' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('tickets')}:
        op.add_column('tickets', sa.Column('onec_item', sa.Text(), nullable=True))

def downgrade():
    op.drop_column('tickets', 'onec_item')
