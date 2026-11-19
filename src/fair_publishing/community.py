"""The open dataset Publications, for a host's Community source to list and import from Zenodo."""
import json
from urllib.parse import urlsplit

from .models import Publication


def open_datasets(session):
    """Open dataset Publications with a DOI and a Zenodo record id, newest first.

    Each is {"id", "title", "description", "license", "doi", "record_id",
    "creators", "published_at"}. Restricted and metadata-only entries, and
    Models, are left out.
    """
    rows = (
        session.query(Publication)
        .filter(Publication.kind == "dataset", Publication.access == "open", Publication.doi != "")
        .order_by(Publication.published_at.desc())
        .all()
    )
    listings = []
    for row in rows:
        record_id = _record_id(row)
        if record_id is None:
            continue
        listings.append(
            {
                "id": row.id,
                "title": row.title,
                "description": row.description,
                "license": row.license,
                "doi": row.doi,
                "record_id": record_id,
                "creators": json.loads(row.creators or "[]"),
                "published_at": row.published_at.isoformat(),
            }
        )
    return listings


def _record_id(publication):
    """The record id from the record's link, else from a Zenodo DOI (10.xxxx/zenodo.<id>)."""
    tail = urlsplit(publication.record_url or "").path.rstrip("/").rsplit("/", 1)[-1]
    if tail.isdigit():
        return tail
    suffix = publication.doi.rsplit("zenodo.", 1)[-1]
    return suffix if suffix.isdigit() else None
