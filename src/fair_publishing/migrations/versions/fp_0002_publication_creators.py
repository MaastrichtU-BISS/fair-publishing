"""The creators of each registry entry, for the Community source.

Adds a column to the registry. Entries published before it have no creators listed.
"""
import sqlalchemy as sa
from alembic import op

revision = "fp_0002_publication_creators"
down_revision = "fp_0001_publications"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("publications", sa.Column("creators", sa.Text(), nullable=False, server_default="[]"))


def downgrade():
    with op.batch_alter_table("publications") as batch:
        batch.drop_column("creators")
