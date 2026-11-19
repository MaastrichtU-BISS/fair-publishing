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
    return {
        c["name"]: (str(c["type"]), c["nullable"], c["default"])
        for c in sa.inspect(engine).get_columns("publications")
    }


def _revision(name, connection):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / name)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    revision.op = Operations(MigrationContext.configure(connection))
    return revision


def test_the_revisions_chain_in_order():
    with sa.create_engine("sqlite://").connect() as connection:
        revisions = [_revision(name, connection) for name in REVISIONS]
    assert revisions[0].down_revision is None
    for previous, revision in zip(revisions, revisions[1:]):
        assert revision.down_revision == previous.revision


def test_the_revisions_match_the_model():
    migrated = sa.create_engine("sqlite://")
    with migrated.begin() as connection:
        for name in REVISIONS:
            _revision(name, connection).upgrade()
    modelled = sa.create_engine("sqlite://")
    Publication.metadata.create_all(modelled)

    assert _columns(migrated) == _columns(modelled)


def test_a_0_1_0_registry_keeps_its_rows_through_the_creators_column_and_back():
    engine = sa.create_engine("sqlite://")
    row = {
        "id": "p1", "owner_id": "u1", "kind": "dataset", "artefact_id": "d1", "title": "T",
        "description": "", "keywords": "", "doi": "10.5072/zenodo.1", "record_url": "https://zenodo.org/records/1",
        "access": "open", "license": "cc-by-4.0", "published_at": "2026-11-01 00:00:00",
    }
    with engine.begin() as connection:
        _revision(REVISIONS[0], connection).upgrade()
        connection.execute(sa.text(f"INSERT INTO publications ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})"), row)
        _revision(REVISIONS[1], connection).upgrade()
        assert connection.execute(sa.text("SELECT id, creators FROM publications")).all() == [("p1", "[]")]
        _revision(REVISIONS[1], connection).downgrade()
        assert connection.execute(sa.text("SELECT id FROM publications")).all() == [("p1",)]
        assert "creators" not in {c["name"] for c in sa.inspect(connection).get_columns("publications")}
