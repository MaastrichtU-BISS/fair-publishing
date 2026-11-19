"""Public file download from Zenodo, against recorded responses served by a real
local HTTP server, so the real transport (redirects, headers, streaming) runs."""
import http.server
import json
import pathlib
import threading
import time

import pytest

import fair_publishing.zenodo as zenodo_module

from fair_publishing import FileTooLarge, RecordNotDownloadable, RepositoryError, Zenodo
from fair_publishing.zenodo import CHUNK_BYTES, _origin

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "zenodo"
DATA = (FIXTURES / "data.csv").read_bytes()
RECORDED_HOST = "https://zenodo.test"


class Server:
    """A local HTTP server answering by path from `routes`; it logs each request."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                server.requests.append({"path": self.path, "headers": dict(self.headers)})
                route = server.routes.get(self.path)
                if route is None:
                    route = (404, {}, b'{"message": "Not found"}')
                if callable(route):
                    return route(self)
                status, headers, body = route
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class Quiet(http.server.ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # a client that hangs up mid-response is expected here

        self.httpd = Quiet(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def paths(self):
        return [entry["path"] for entry in self.requests]

    def record(self, fixture, status=200):
        """Serve a recorded record, its links moved onto this server."""
        text = (FIXTURES / fixture).read_text().replace(RECORDED_HOST, self.base)
        record = json.loads(text)
        self.routes[f"/api/records/{record['id']}"] = (status, {"Content-Type": "application/json"}, text.encode())
        return record

    def file(self, record_id, body=DATA, status=200, headers=None, key="data.csv"):
        path = f"/api/records/{record_id}/files/{key}/content"
        self.routes[path] = (status, headers or {}, body)
        return path

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def zenodo():
    server = Server()
    yield server
    server.close()


@pytest.fixture
def elsewhere():
    server = Server()
    yield server
    server.close()


@pytest.fixture
def public(zenodo):
    return Zenodo(zenodo.base)


@pytest.mark.parametrize(
    ("fixture", "record_id"),
    [("record_open.json", 101), ("record_open_entries.json", 102), ("record_open_legacy.json", 106)],
    ids=["legacy-list", "rdm-entries", "legacy-real-capture"],
)
def test_an_open_record_lists_its_file_and_downloads_it(zenodo, public, fixture, record_id):
    zenodo.record(fixture)
    path = zenodo.file(record_id)

    listing = public.record_files(record_id)

    assert listing["downloadable"] is True
    assert listing["access"] == {"record": "public", "files": "public"}
    [file] = listing["files"]
    assert file["key"] == "data.csv" and file["url"] == zenodo.base + path
    assert public.download(record_id, "data.csv", max_bytes=1024) == DATA


def test_the_record_is_asked_for_in_its_rdm_shape_and_unencoded(zenodo, public):
    zenodo.record("record_open_entries.json")

    public.record_files(102)

    headers = zenodo.requests[0]["headers"]
    assert headers["Accept"] == "application/vnd.inveniordm.v1+json"
    assert headers["Accept-Encoding"] == "identity"


def test_no_token_or_netrc_credentials_are_sent(zenodo, tmp_path, monkeypatch):
    netrc = tmp_path / ".netrc"
    netrc.write_text("machine 127.0.0.1 login someone password secret\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HOME", str(tmp_path))
    zenodo.record("record_open.json")
    zenodo.file(101)

    Zenodo(zenodo.base, token="zen-secret").download(101, "data.csv", max_bytes=1024)

    assert len(zenodo.requests) == 2
    assert all("Authorization" not in entry["headers"] for entry in zenodo.requests)


@pytest.mark.parametrize(
    "fixture",
    ["record_restricted.json", "record_metadata_only.json", "record_restricted_legacy.json"],
    ids=["restricted", "metadata-only", "restricted-legacy"],
)
def test_restricted_and_metadata_only_records_are_not_downloadable(zenodo, public, fixture):
    record = zenodo.record(fixture)

    listing = public.record_files(record["id"])

    assert listing["downloadable"] is False
    assert listing["files"] == []
    with pytest.raises(RecordNotDownloadable, match="This record is no longer downloadable"):
        public.download(record["id"], "data.csv", max_bytes=1024)
    assert not any("/files/" in path for path in zenodo.paths())


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_a_record_that_is_gone_or_no_longer_public_is_not_downloadable(zenodo, public, status):
    zenodo.routes["/api/records/101"] = (status, {}, (FIXTURES / "gone.json").read_bytes())

    with pytest.raises(RecordNotDownloadable, match="This record is no longer downloadable"):
        public.download(101, "data.csv", max_bytes=1024)


@pytest.mark.parametrize("status", [403, 410])
def test_a_file_that_is_gone_or_no_longer_public_is_not_downloadable(zenodo, public, status):
    zenodo.record("record_open.json")
    zenodo.file(101, body=b"{}", status=status)

    with pytest.raises(RecordNotDownloadable, match="This record is no longer downloadable"):
        public.download(101, "data.csv", max_bytes=1024)


def test_a_key_the_record_does_not_have_is_not_downloadable(zenodo, public):
    zenodo.record("record_open.json")

    with pytest.raises(RecordNotDownloadable):
        public.download(101, "missing.csv", max_bytes=1024)
    assert not any("/files/" in path for path in zenodo.paths())


@pytest.mark.parametrize("record_id", ["1/../../../login?next=x", "12a", "", "١٢"])
def test_a_record_id_that_is_not_a_number_is_refused_without_a_request(zenodo, public, record_id):
    with pytest.raises(RepositoryError, match="not a Zenodo record id"):
        public.record_files(record_id)
    assert zenodo.requests == []


def test_a_file_link_off_zenodo_is_not_listed_and_never_requested(zenodo, elsewhere, public):
    record = json.loads((FIXTURES / "record_open_entries.json").read_text())
    record["files"]["entries"]["data.csv"]["links"]["content"] = f"{elsewhere.base}/data.csv"
    zenodo.routes["/api/records/102"] = (200, {}, json.dumps(record).encode())

    assert public.record_files(102)["downloadable"] is False
    with pytest.raises(RecordNotDownloadable):
        public.download(102, "data.csv", max_bytes=1024)
    assert elsewhere.requests == []


@pytest.mark.parametrize(
    "link",
    [
        "{elsewhere}\\@{host}/data.csv",  # urlsplit and urllib3 disagree on the host
        "http://user:pass@{host}/data.csv",
        "http://{host}/data .csv",
    ],
    ids=["backslash", "userinfo", "whitespace"],
)
def test_an_ambiguous_file_link_is_refused(zenodo, elsewhere, public, link):
    host = zenodo.base.removeprefix("http://")
    record = json.loads((FIXTURES / "record_open_entries.json").read_text())
    record["files"]["entries"]["data.csv"]["links"]["content"] = link.format(elsewhere=elsewhere.base, host=host)
    zenodo.routes["/api/records/102"] = (200, {}, json.dumps(record).encode())

    with pytest.raises(RecordNotDownloadable):
        public.download(102, "data.csv", max_bytes=1024)
    assert zenodo.paths() == ["/api/records/102"]
    assert elsewhere.requests == []


def test_a_relative_file_link_is_on_zenodo(zenodo, public):
    record = json.loads((FIXTURES / "record_open_entries.json").read_text())
    record["files"]["entries"]["data.csv"]["links"]["content"] = "/api/records/102/files/data.csv/content"
    zenodo.routes["/api/records/102"] = (200, {}, json.dumps(record).encode())
    zenodo.file(102)

    assert public.download(102, "data.csv", max_bytes=1024) == DATA


@pytest.mark.parametrize("target", ["file", "record"])
def test_a_redirect_off_zenodo_is_refused_and_never_followed(zenodo, elsewhere, public, target):
    elsewhere.routes["/data"] = (200, {}, DATA)
    if target == "record":
        zenodo.routes["/api/records/101"] = (302, {"Location": f"{elsewhere.base}/data"}, b"")
    else:
        zenodo.record("record_open.json")
        zenodo.file(101, body=b"", status=302, headers={"Location": f"{elsewhere.base}/data"})

    with pytest.raises(RepositoryError, match="somewhere other than Zenodo"):
        public.download(101, "data.csv", max_bytes=1024)
    assert elsewhere.requests == []


def test_redirects_on_zenodo_are_followed_for_records_and_files(zenodo, public):
    # Zenodo redirects a concept record to its latest version.
    zenodo.routes["/api/records/100"] = (301, {"Location": "/api/records/101"}, b"")
    zenodo.record("record_open.json")
    zenodo.file(101, body=b"", status=302, headers={"Location": "/api/files/bucket-101/data.csv"})
    zenodo.routes["/api/files/bucket-101/data.csv"] = (200, {}, DATA)

    assert public.download(100, "data.csv", max_bytes=1024) == DATA


def test_a_redirect_body_is_not_read(zenodo, public):
    """A redirect whose body never ends must not stall or fill memory."""

    release = threading.Event()

    def endless_redirect(handler):
        handler.send_response(302)
        handler.send_header("Location", "/api/files/bucket-101/data.csv")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        try:
            while not release.is_set():
                handler.wfile.write(b"10000\r\n" + b"x" * 0x10000 + b"\r\n")
            handler.wfile.write(b"0\r\n\r\n")  # end the body, so a regressed client finishes
        except OSError:
            pass

    zenodo.record("record_open.json")
    zenodo.routes["/api/records/101/files/data.csv/content"] = endless_redirect
    zenodo.routes["/api/files/bucket-101/data.csv"] = (200, {}, DATA)
    result = {}
    worker = threading.Thread(
        target=lambda: result.update(body=public.download(101, "data.csv", max_bytes=1024)), daemon=True
    )
    worker.start()
    try:
        worker.join(timeout=10)
        finished = not worker.is_alive()
    finally:
        release.set()
        worker.join(timeout=30)

    assert finished, "the redirect body was read"
    assert result["body"] == DATA


def test_too_many_redirects_are_refused(zenodo, public):
    zenodo.routes["/api/records/101"] = (302, {"Location": "/api/records/101"}, b"")

    with pytest.raises(RepositoryError, match="redirected too many times"):
        public.record_files(101)


def test_a_redirect_without_a_location_is_refused(zenodo, public):
    zenodo.routes["/api/records/101"] = (302, {}, b"")

    with pytest.raises(RepositoryError, match="without saying where"):
        public.record_files(101)


def test_a_file_declared_over_max_bytes_in_the_record_is_refused_before_it_is_fetched(zenodo, public):
    zenodo.record("record_open.json")
    path = zenodo.file(101)

    with pytest.raises(FileTooLarge, match="larger than the 10-byte limit"):
        public.download(101, "data.csv", max_bytes=10)
    assert path not in zenodo.paths()


def test_a_content_length_over_max_bytes_is_refused_before_the_body_is_read(zenodo, public):
    zenodo.record("record_open_legacy.json")  # its file has no size
    zenodo.file(106, body=DATA, headers={"Content-Length": str(len(DATA))})

    with pytest.raises(FileTooLarge):
        public.download(106, "data.csv", max_bytes=10)


def test_a_file_that_streams_past_max_bytes_is_cut_off(zenodo, public):
    zenodo.record("record_open_legacy.json")
    body = b"x" * (CHUNK_BYTES * 3)

    def unsized(handler):
        handler.send_response(200)
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)

    zenodo.routes["/api/records/106/files/data.csv/content"] = unsized

    with pytest.raises(FileTooLarge):
        public.download(106, "data.csv", max_bytes=CHUNK_BYTES + 1)


def test_a_malformed_content_length_falls_back_to_the_streaming_cap(zenodo, public):
    zenodo.record("record_open_legacy.json")

    def malformed(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "abc")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(DATA)

    zenodo.routes["/api/records/106/files/data.csv/content"] = malformed

    assert public.download(106, "data.csv", max_bytes=len(DATA)) == DATA


def test_a_compressed_file_is_refused(zenodo, public):
    zenodo.record("record_open_legacy.json")
    zenodo.file(106, body=b"\x1f\x8b", headers={"Content-Encoding": "gzip"})

    with pytest.raises(RepositoryError, match="gzip-encoded"):
        public.download(106, "data.csv", max_bytes=1024)


def test_a_file_exactly_max_bytes_long_is_allowed(zenodo, public):
    zenodo.record("record_open.json")
    zenodo.file(101)

    assert public.download(101, "data.csv", max_bytes=len(DATA)) == DATA


def test_a_refused_record_read_is_a_repository_error(zenodo, public):
    zenodo.routes["/api/records/101"] = (500, {}, b'{"message": "Internal error"}')

    with pytest.raises(RepositoryError, match="refused the request: 500"):
        public.record_files(101)


def test_a_record_read_that_does_not_connect_is_a_repository_error():
    server = Server()
    base = server.base
    server.close()

    with pytest.raises(RepositoryError, match="Zenodo did not answer"):
        Zenodo(base).record_files(101)


def test_origins_compare_default_ports_and_case():
    assert _origin("https://Zenodo.org:443/a") == _origin("https://zenodo.org/b")
    assert _origin("https://zenodo.org:8443/a") != _origin("https://zenodo.org/a")
    assert _origin("/relative") is None


def test_a_path_relative_file_link_resolves_against_the_record_as_served(zenodo, public):
    zenodo.routes["/api/records/108"] = (301, {"Location": "/api/records/108/"}, b"")
    record = json.loads((FIXTURES / "record_open_entries.json").read_text())
    record["id"] = 108
    record["files"]["entries"]["data.csv"]["links"]["content"] = "files/data.csv/content"
    zenodo.routes["/api/records/108/"] = (200, {}, json.dumps(record).encode())
    zenodo.routes["/api/records/108/files/data.csv/content"] = (200, {}, DATA)

    assert public.download(108, "data.csv", max_bytes=1024) == DATA


@pytest.mark.parametrize("target", ["record", "file"])
def test_a_malformed_redirect_location_is_a_repository_error(zenodo, public, target):
    bad = (302, {"Location": "http://[not-a-host/x"}, b"")
    if target == "record":
        zenodo.routes["/api/records/101"] = bad
    else:
        zenodo.record("record_open.json")
        zenodo.routes["/api/records/101/files/data.csv/content"] = bad

    with pytest.raises(RepositoryError, match="not a URL|somewhere other than Zenodo"):
        public.download(101, "data.csv", max_bytes=1024)


@pytest.mark.parametrize("body", [b"[1, 2]", b"null", b'"error"'])
def test_a_record_that_is_not_a_json_object_is_a_repository_error(zenodo, public, body):
    zenodo.routes["/api/records/101"] = (200, {}, body)

    with pytest.raises(RepositoryError, match="not a JSON object"):
        public.record_files(101)


@pytest.mark.parametrize(
    "entry",
    [
        {"key": "data.csv", "links": ["x"]},
        {"key": "data.csv", "links": {"content": {"href": "x"}}},
        {"key": "data.csv", "size": "5", "links": {"content": "/api/records/109/files/data.csv/content"}},
        {"key": ["data.csv"], "links": {"content": "/api/records/109/files/data.csv/content"}},
    ],
    ids=["links-list", "link-not-text", "size-text", "key-not-text"],
)
def test_a_malformed_file_entry_is_not_an_unexpected_error(zenodo, public, entry):
    record = {"id": 109, "access": {"record": "public", "files": "public"}, "metadata": [], "files": [entry]}
    zenodo.routes["/api/records/109"] = (200, {}, json.dumps(record).encode())
    zenodo.routes["/api/records/109/files/data.csv/content"] = (200, {}, DATA)

    public.record_files(109)  # no AttributeError or TypeError
    try:
        assert public.download(109, "data.csv", max_bytes=1024) == DATA
    except RecordNotDownloadable:
        pass


def _bounded(call, release, seconds=10):
    """Run call() on a daemon worker; release the server and give up after
    `seconds`, so a regression fails the test instead of stalling the run."""
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        worker.join(timeout=seconds)
        finished = not worker.is_alive()
    finally:
        release.set()
    assert finished, "the read ran past its deadline"
    return outcome


def test_a_trickled_body_stops_at_the_deadline(zenodo, public, monkeypatch):
    monkeypatch.setattr(zenodo_module, "DEADLINE_SECONDS", 0.5)
    release = threading.Event()

    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            while not release.is_set():
                handler.wfile.write(b"x")
                handler.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    zenodo.record("record_open_legacy.json")
    zenodo.routes["/api/records/106/files/data.csv/content"] = trickle

    outcome = _bounded(lambda: public.download(106, "data.csv", max_bytes=1024), release)

    assert isinstance(outcome.get("error"), RepositoryError) and "took too long" in str(outcome["error"])


@pytest.mark.parametrize("body", [b"", DATA], ids=["empty-body", "short-body"])
def test_slow_headers_stop_at_the_deadline(zenodo, public, monkeypatch, body):
    monkeypatch.setattr(zenodo_module, "DEADLINE_SECONDS", 0.5)
    release = threading.Event()

    def slow(handler):
        release.wait(5)  # headers arrive well after the deadline
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    zenodo.routes["/api/records/101"] = slow

    # The server holds the headers for 5 s; capped timeouts give up near the
    # 0.5 s deadline, well inside the 2 s bound.
    outcome = _bounded(lambda: public.record_files(101), release, seconds=2)

    assert isinstance(outcome.get("error"), RepositoryError)


def test_a_body_finished_after_the_deadline_is_not_returned():
    class Finished:
        headers = {}

        def read1(self, amount, decode_content=False):
            return b""

    with pytest.raises(RepositoryError, match="took too long"):
        zenodo_module._read_capped(Finished(), max_bytes=1024, deadline=time.monotonic() - 1)


@pytest.mark.parametrize(
    "files",
    [5, True, 1.5, "data.csv", {"entries": ["x"]}, {"entries": "bad"}, {"entries": 3}, None],
    ids=["int", "bool", "float", "text", "entries-list", "entries-text", "entries-int", "null"],
)
def test_a_malformed_file_collection_lists_no_files(zenodo, public, files):
    record = {"id": 110, "access": {"record": "public", "files": "public"}, "files": files}
    zenodo.routes["/api/records/110"] = (200, {}, json.dumps(record).encode())

    listing = public.record_files(110)

    assert listing["downloadable"] is False and listing["files"] == []


def test_a_file_without_a_usable_key_is_not_listed(zenodo, public):
    entry = {"key": ["data.csv"], "links": {"content": "/api/records/111/files/data.csv/content"}}
    record = {"id": 111, "access": {"record": "public", "files": "public"}, "files": [entry]}
    zenodo.routes["/api/records/111"] = (200, {}, json.dumps(record).encode())

    assert public.record_files(111)["downloadable"] is False
    with pytest.raises(RecordNotDownloadable):
        public.download(111, None, max_bytes=1024)


@pytest.mark.parametrize("access", [[], "public", None, 1])
def test_a_malformed_access_block_is_never_open_even_with_a_legacy_open_flag(zenodo, public, access):
    record = json.loads((FIXTURES / "record_open_legacy.json").read_text().replace(RECORDED_HOST, zenodo.base))
    record["access"] = access
    zenodo.routes["/api/records/106"] = (200, {}, json.dumps(record).encode())
    zenodo.file(106)

    assert public.record_files(106)["downloadable"] is False
    with pytest.raises(RecordNotDownloadable):
        public.download(106, "data.csv", max_bytes=1024)


def test_the_installed_urllib3_has_what_public_reads_need():
    import inspect

    import urllib3

    assert "decode_content" in inspect.signature(urllib3.HTTPResponse.read1).parameters
