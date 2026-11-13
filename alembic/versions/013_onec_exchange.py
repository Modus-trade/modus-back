"""1C directory cache, exchange log and linked documents."""
from alembic import op
import sqlalchemy as sa
from app.onec_models import OneCSettings, OneCRecord, OneCExchange
revision = '013'
down_revision = '012'

def upgrade():
    for model in (OneCSettings, OneCRecord, OneCExchange):
        model.__table__.create(op.get_bind(), checkfirst=True)
    if 'onec_document' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('tickets')}:
        op.add_column('tickets', sa.Column('onec_document', sa.Text(), nullable=True))

def downgrade():
    op.drop_column('tickets', 'onec_document')
    for model in (OneCExchange, OneCRecord, OneCSettings):
        model.__table__.drop(op.get_bind())
