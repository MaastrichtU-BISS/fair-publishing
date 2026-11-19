"""The shipped revision builds the table the model describes."""
import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from fair_publishing.models import Publication

VERSIONS = pathlib.Path(__file__).parent.parent / "src/fair_publishing/migrations/versions"
REVISIONS = ["fp_0001_publications.py", "fp_0002_publication_creators.py"]


def _columns(engine):
    return {c["name"]: (str(c["type"]), c["nullable"]) for c in sa.inspect(engine).get_columns("publications")}


def test_the_revisions_match_the_model():
    migrated = sa.create_engine("sqlite://")
    with migrated.begin() as connection:
        for name in REVISIONS:
            spec = importlib.util.spec_from_file_location(name, VERSIONS / name)
            revision = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(revision)
            revision.op = Operations(MigrationContext.configure(connection))
            revision.upgrade()
    modelled = sa.create_engine("sqlite://")
    Publication.metadata.create_all(modelled)

    assert _columns(migrated) == _columns(modelled)
