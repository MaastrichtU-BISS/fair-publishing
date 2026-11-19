from datetime import datetime

from fair_publishing import open_datasets
from fair_publishing.models import Publication
from tests.test_publish import connected, dataset, publish  # noqa: F401


def row(id, kind="dataset", access="open", doi="10.5072/zenodo.1", record_url="https://zenodo.test/records/1", **fields):
    return Publication(
        id=id,
        owner_id="alice",
        kind=kind,
        artefact_id=id,
        title=f"Title {id}",
        description="Description",
        keywords="",
        doi=doi,
        record_url=record_url,
        access=access,
        license="cc-by-4.0",
        published_at=fields.pop("published_at", datetime(2026, 11, 1)),
        **fields,
    )


def test_only_open_datasets_with_a_record_are_listed(app):
    session = app.extensions["fair_publishing"].db.session
    with app.app_context():
        session.add_all(
            [
                row("open", doi="10.5072/zenodo.201", record_url="https://zenodo.test/records/201"),
                row("restricted", access="restricted", doi="10.5072/zenodo.202", record_url="https://zenodo.test/records/202"),
                row("metadata-only", access="metadata_only", doi="10.5072/zenodo.203", record_url="https://zenodo.test/records/203"),
                row("model", kind="model", doi="10.5072/zenodo.204", record_url="https://zenodo.test/records/204"),
                row("no-doi", doi="", record_url=""),
                row("no-record-id", doi="10.1234/elsewhere", record_url="https://example.org/paper"),
            ]
        )
        session.commit()

        assert [item["id"] for item in open_datasets(session)] == ["open"]


def test_a_listing_carries_the_record_and_its_creators(app, client, dataset, connected, zenodo):  # noqa: F811
    publish(client)
    session = app.extensions["fair_publishing"].db.session
    with app.app_context():
        [item] = open_datasets(session)

    assert item["title"] == "Tenancy clauses"
    assert item["description"] == "Labelled clauses from tenancy agreements."
    assert item["license"] == "cc-by-4.0"
    assert item["doi"] == "10.5072/zenodo.101"
    assert item["record_id"] == "101"
    assert item["creators"] == [
        {"name": "Doe, Alice", "affiliation": "Maastricht University", "orcid": "0000-0002-1825-0097"}
    ]
    assert datetime.fromisoformat(item["published_at"]).tzinfo is None


def test_the_record_id_falls_back_to_the_doi(app):
    session = app.extensions["fair_publishing"].db.session
    with app.app_context():
        session.add(row("by-doi", doi="10.5281/zenodo.301", record_url="https://zenodo.org/record/"))
        session.commit()

        [item] = open_datasets(session)

    assert item["record_id"] == "301"
    assert item["creators"] == []


def test_the_newest_come_first(app):
    session = app.extensions["fair_publishing"].db.session
    with app.app_context():
        session.add_all(
            [
                row("older", published_at=datetime(2026, 10, 1), doi="10.5072/zenodo.401", record_url="https://zenodo.test/records/401"),
                row("newer", published_at=datetime(2026, 11, 1), doi="10.5072/zenodo.402", record_url="https://zenodo.test/records/402"),
            ]
        )
        session.commit()

        assert [item["id"] for item in open_datasets(session)] == ["newer", "older"]
