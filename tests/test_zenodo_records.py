"""Public file download from Zenodo, against recorded responses at the HTTP boundary."""
import json
import pathlib

import pytest
import requests

import fair_publishing.zenodo as zenodo_module
from fair_publishing import RecordNotDownloadable, RepositoryError, Zenodo
from fair_publishing.zenodo import FileTooLarge

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "zenodo"
BASE = "https://zenodo.test"
DATA = (FIXTURES / "data.csv").read_bytes()
OPEN_CONTENT = f"{BASE}/api/records/101/files/data.csv/content"


def recorded(name):
    return json.loads((FIXTURES / name).read_text())


class Reply:
    def __init__(self, status, body=None, headers=None, content=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._content = content
        self.closed = False

    def json(self):
        return self._body

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeZenodoGet:
    """Answers GET by URL with recorded replies; anything unrecorded is a 404."""

    def __init__(self):
        self.calls = []
        self.replies = {}
        self.error = None

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        if url in self.replies:
            return self.replies[url]
        return Reply(404, {"message": "Not found"})

    def urls(self):
        return [call["url"] for call in self.calls]

    def serve_record(self, fixture, status=200):
        record = recorded(fixture)
        self.replies[f"{BASE}/api/records/{record['id']}"] = Reply(status, record)
        return record


@pytest.fixture
def http(monkeypatch):
    fake = FakeZenodoGet()
    monkeypatch.setattr(zenodo_module.requests, "get", fake.get)
    return fake


@pytest.fixture
def public():
    return Zenodo(BASE)


@pytest.mark.parametrize(
    ("fixture", "record_id", "file_url"),
    [
        ("record_open.json", 101, f"{BASE}/api/records/101/files/data.csv/content"),
        ("record_open_entries.json", 102, f"{BASE}/api/records/102/files/data.csv/content"),
    ],
    ids=["list-shape", "entries-shape"],
)
def test_an_open_record_lists_its_file_and_downloads_it(http, public, fixture, record_id, file_url):
    http.serve_record(fixture)
    http.replies[file_url] = Reply(200, content=DATA, headers={"Content-Length": str(len(DATA))})

    listing = public.record_files(record_id)

    assert listing["downloadable"] is True
    assert listing["access"] == {"record": "public", "files": "public"}
    [file] = listing["files"]
    assert file["key"] == "data.csv" and file["size"] == 29 and file["url"] == file_url
    assert file["checksum"].endswith("840e326511d9a9dcadd5bd2e6f2f5481")
    assert public.download(record_id, "data.csv", max_bytes=1024) == DATA


def test_no_token_is_sent_to_read_or_fetch_a_public_file(http):
    http.serve_record("record_open.json")
    http.replies[OPEN_CONTENT] = Reply(200, content=DATA)
    client = Zenodo(BASE, token="zen-secret")

    client.download(101, "data.csv", max_bytes=1024)

    assert len(http.calls) == 2
    assert all("Authorization" not in (call.get("headers") or {}) for call in http.calls)


@pytest.mark.parametrize(
    "fixture",
    ["record_restricted.json", "record_metadata_only.json"],
    ids=["restricted", "metadata-only"],
)
def test_restricted_and_metadata_only_records_are_not_downloadable(http, public, fixture):
    record = http.serve_record(fixture)

    listing = public.record_files(record["id"])

    assert listing["downloadable"] is False
    assert listing["files"] == []
    with pytest.raises(RecordNotDownloadable, match="This record is no longer downloadable"):
        public.download(record["id"], "data.csv", max_bytes=1024)
    assert not any("/files/" in url for url in http.urls())


@pytest.mark.parametrize("status", [404, 410])
def test_a_record_that_is_gone_is_not_downloadable(http, public, status):
    http.replies[f"{BASE}/api/records/101"] = Reply(status, recorded("gone.json"))

    with pytest.raises(RecordNotDownloadable, match="This record is no longer downloadable"):
        public.download(101, "data.csv", max_bytes=1024)


def test_a_file_that_has_gone_is_not_downloadable(http, public):
    http.serve_record("record_open.json")
    http.replies[OPEN_CONTENT] = Reply(410, recorded("gone.json"))

    with pytest.raises(RecordNotDownloadable, match="This record is no longer downloadable"):
        public.download(101, "data.csv", max_bytes=1024)


def test_a_key_the_record_does_not_have_is_not_downloadable(http, public):
    http.serve_record("record_open.json")

    with pytest.raises(RecordNotDownloadable, match="This record is no longer downloadable"):
        public.download(101, "missing.csv", max_bytes=1024)
    assert not any("/files/" in url for url in http.urls())


def test_a_link_off_zenodo_is_refused_and_never_requested(http, public):
    http.serve_record("record_off_host.json")

    with pytest.raises(RepositoryError, match="somewhere other than Zenodo"):
        public.download(105, "data.csv", max_bytes=1024)
    assert all(url.startswith(BASE) for url in http.urls())


def test_a_redirect_off_zenodo_is_refused(http, public):
    http.serve_record("record_open.json")
    http.replies[OPEN_CONTENT] = Reply(302, headers={"Location": "https://files.example.org/data.csv"})

    with pytest.raises(RepositoryError, match="somewhere other than Zenodo"):
        public.download(101, "data.csv", max_bytes=1024)
    assert all(url.startswith(BASE) for url in http.urls())


def test_a_redirect_on_zenodo_is_followed(http, public):
    http.serve_record("record_open.json")
    storage = f"{BASE}/api/files/bucket-101/data.csv"
    http.replies[OPEN_CONTENT] = Reply(302, headers={"Location": "/api/files/bucket-101/data.csv"})
    http.replies[storage] = Reply(200, content=DATA)

    assert public.download(101, "data.csv", max_bytes=1024) == DATA


def test_a_file_declared_over_max_bytes_is_refused_before_it_is_fetched(http, public):
    http.serve_record("record_open.json")
    http.replies[OPEN_CONTENT] = Reply(200, content=DATA)

    with pytest.raises(FileTooLarge, match="larger than the 10-byte limit"):
        public.download(101, "data.csv", max_bytes=10)
    assert OPEN_CONTENT not in http.urls()


def test_a_file_that_streams_past_max_bytes_is_cut_off(http, public):
    record = http.serve_record("record_open.json")
    del record["files"][0]["filesize"]
    http.replies[OPEN_CONTENT] = Reply(200, content=DATA)

    with pytest.raises(FileTooLarge):
        public.download(101, "data.csv", max_bytes=10)


def test_a_file_exactly_max_bytes_long_is_allowed(http, public):
    http.serve_record("record_open.json")
    http.replies[OPEN_CONTENT] = Reply(200, content=DATA)

    assert public.download(101, "data.csv", max_bytes=len(DATA)) == DATA


def test_a_refused_record_read_is_a_repository_error(http, public):
    http.replies[f"{BASE}/api/records/101"] = Reply(500, {"message": "Internal error"})

    with pytest.raises(RepositoryError, match="refused to read the record: Internal error"):
        public.record_files(101)


def test_a_record_read_that_does_not_connect_is_a_repository_error(http, public):
    http.error = requests.ConnectionError("no route")

    with pytest.raises(RepositoryError, match="Zenodo did not answer: ConnectionError"):
        public.record_files(101)


def test_the_record_is_asked_for_in_its_rdm_shape(http, public):
    http.serve_record("record_open_entries.json")

    public.record_files(102)

    assert http.calls[0]["headers"] == {"Accept": "application/vnd.inveniordm.v1+json"}


def test_an_open_record_in_the_legacy_shape_downloads(http, public):
    # Trimmed from a real capture: no `access` block, openness in
    # metadata.access_right, and the file's content link under `self`.
    http.serve_record("record_open_legacy.json")
    content = f"{BASE}/api/records/103/files/data.csv/content"
    http.replies[content] = Reply(200, content=DATA)

    record = public.record_files(103)

    assert record["downloadable"] is True
    assert record["access"] == {"record": "public", "files": "public"}
    assert record["files"][0]["url"] == content
    assert public.download(103, "data.csv", max_bytes=len(DATA)) == DATA


def test_a_restricted_record_in_the_legacy_shape_is_not_downloadable(http, public):
    http.serve_record("record_restricted_legacy.json")

    assert public.record_files(104)["downloadable"] is False
    with pytest.raises(RecordNotDownloadable):
        public.download(104, "data.csv", max_bytes=len(DATA))
