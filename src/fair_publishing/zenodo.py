"""Zenodo's deposit API, with the user's own personal access token, and its
public records API, which needs no token."""
import json
import re
import time
from urllib.parse import quote, urljoin

import requests
import urllib3
from urllib3.util import parse_url

TIMEOUT_SECONDS = 120
CHUNK_BYTES = 64 * 1024
MAX_REDIRECTS = 3
RECORD_MAX_BYTES = 10 * 1024 * 1024
# The whole read, not one socket read: a server trickling bytes cannot hold a worker longer.
DEADLINE_SECONDS = 600
# Anonymous reads of a record or file that is gone or no longer public.
NOT_DOWNLOADABLE_STATUSES = (401, 403, 404, 410)
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
DEFAULT_PORTS = {"http": 80, "https": 443}
RECORD_ID = re.compile(r"[0-9]+")
# Ask for the InvenioRDM shape (an `access` block, files as entries with a
# `content` link); without it Zenodo answers in its legacy shape, which _access
# and _file read as well. Identity encoding keeps the size cap on the bytes
# that actually arrive: no compressed body expands past it.
RECORD_HEADERS = {"Accept": "application/vnd.inveniordm.v1+json", "Accept-Encoding": "identity"}
FILE_HEADERS = {"Accept-Encoding": "identity"}


class RepositoryError(Exception):
    """The repository refused the deposit or did not answer."""


class RecordNotDownloadable(RepositoryError):
    """The record is gone, no longer open, or its file is missing."""

    def __init__(self):
        super().__init__("This record is no longer downloadable")


class FileTooLarge(RepositoryError):
    """The file is bigger than the caller allowed."""

    def __init__(self, max_bytes):
        super().__init__(f"This file is larger than the {max_bytes}-byte limit")


class Zenodo:
    def __init__(self, base_url, token=None):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def _check(self, response, step):
        if response.status_code >= 300:
            try:
                message = response.json().get("message")
            except ValueError:
                message = None
            raise RepositoryError(f"Zenodo refused to {step}: {message or response.status_code}")
        return response.json()

    def deposit(self, metadata, files):
        """Create a deposition, upload the files, set the metadata and publish.

        Returns {"doi", "record_url", "record_id"}.
        """
        try:
            created = self._check(
                requests.post(f"{self.base_url}/api/deposit/depositions", json={}, headers=self.headers, timeout=TIMEOUT_SECONDS),
                "create the deposit",
            )
            deposition = created["id"]
            bucket = created["links"]["bucket"]
            for file in files:
                self._check(
                    requests.put(
                        f"{bucket}/{quote(file.name)}",
                        data=file.content,
                        headers={**self.headers, "Content-Type": "application/octet-stream"},
                        timeout=TIMEOUT_SECONDS,
                    ),
                    f"take {file.name}",
                )
            self._check(
                requests.put(
                    f"{self.base_url}/api/deposit/depositions/{deposition}",
                    json={"metadata": metadata},
                    headers=self.headers,
                    timeout=TIMEOUT_SECONDS,
                ),
                "take the metadata",
            )
            published = self._check(
                requests.post(
                    f"{self.base_url}/api/deposit/depositions/{deposition}/actions/publish",
                    headers=self.headers,
                    timeout=TIMEOUT_SECONDS,
                ),
                "publish",
            )
        except requests.RequestException as exc:
            raise RepositoryError(f"Zenodo did not answer: {type(exc).__name__}") from exc
        links = published.get("links", {})
        return {
            "doi": published["doi"],
            "record_url": links.get("record_html") or links.get("html"),
            "record_id": str(published["id"]),
        }

    def record_files(self, record_id):
        """A record's access and files, read without a token.

        Returns {"record_id", "access", "downloadable", "files"}; each file is
        {"key", "size", "checksum", "url"}. A record that is not open to everyone,
        or has no file to fetch, is not downloadable and lists none. Raises
        RecordNotDownloadable when Zenodo no longer serves the record.
        """
        if not RECORD_ID.fullmatch(str(record_id)):
            raise RepositoryError("That is not a Zenodo record id")
        body, record_url = self._public_get(f"{self.base_url}/api/records/{record_id}", RECORD_HEADERS, RECORD_MAX_BYTES)
        try:
            record = json.loads(body)
        except ValueError as exc:
            raise RepositoryError("Zenodo sent a record that is not JSON") from exc
        if not isinstance(record, dict):
            raise RepositoryError("Zenodo sent a record that is not a JSON object")
        access = _access(record)
        # Relative links resolve against the record as served (after any redirect);
        # a link off Zenodo is left out, since it would never be fetched.
        files = [_file(entry, record_url) for entry in _entries(record.get("files"))]
        files = [
            entry for entry in files
            if entry["key"] and entry["url"] and _origin(entry["url"]) == _origin(self.base_url)
        ]
        downloadable = access["record"] == "public" and access["files"] == "public" and bool(files)
        return {
            "record_id": str(record.get("id", record_id)),
            "access": access,
            "downloadable": downloadable,
            "files": files if downloadable else [],
        }

    def download(self, record_id, key, max_bytes):
        """The bytes of one public file of an open record, at most max_bytes long."""
        record = self.record_files(record_id)
        if not record["downloadable"]:
            raise RecordNotDownloadable()
        file = next((entry for entry in record["files"] if entry["key"] == key), None)
        if file is None:
            raise RecordNotDownloadable()
        if file["size"] is not None and file["size"] > max_bytes:
            raise FileTooLarge(max_bytes)
        return self._public_get(file["url"], FILE_HEADERS, max_bytes)[0]

    def _public_get(self, url, headers, max_bytes):
        """GET a public URL on the configured Zenodo host and return its body and
        the URL that served it.

        Uses urllib3 rather than requests, on purpose: the host is checked with
        the same parser that sends the request, no .netrc credentials are added,
        and a redirect is not followed or its body read. Each redirect target is
        checked again before it is requested.

        Proxies and CA bundles set for requests (HTTPS_PROXY, REQUESTS_CA_BUNDLE)
        do not apply here; urllib3 uses the system trust store and connects
        directly."""
        deadline = time.monotonic() + DEADLINE_SECONDS
        with urllib3.PoolManager(retries=False) as http:
            return self._follow(http, url, headers, max_bytes, deadline)

    def _follow(self, http, url, headers, max_bytes, deadline):
        for _ in range(MAX_REDIRECTS + 1):
            if _origin(url) is None or _origin(url) != _origin(self.base_url):
                raise RepositoryError("Zenodo linked a file somewhere other than Zenodo, so it was not fetched")
            # Each hop's connect and read timeouts are capped at the time left, so
            # slow headers or a stalled body cannot run far past the deadline.
            left = _time_left(deadline)
            try:
                response = http.request(
                    "GET", url, headers=headers, redirect=False, preload_content=False, decode_content=False,
                    timeout=urllib3.Timeout(connect=min(30, left), read=min(TIMEOUT_SECONDS, left)),
                )
            except urllib3.exceptions.HTTPError as exc:
                raise RepositoryError(f"Zenodo did not answer: {type(exc).__name__}") from exc
            try:
                if response.status in NOT_DOWNLOADABLE_STATUSES:
                    raise RecordNotDownloadable()
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise RepositoryError("Zenodo redirected without saying where")
                    try:
                        url = urljoin(url, location)
                    except ValueError as exc:
                        raise RepositoryError("Zenodo redirected to an address that is not a URL") from exc
                    continue
                if response.status >= 300:
                    raise RepositoryError(f"Zenodo refused the request: {response.status}")
                return _read_capped(response, max_bytes, deadline), url
            except urllib3.exceptions.HTTPError as exc:
                raise RepositoryError(f"Zenodo did not answer: {type(exc).__name__}") from exc
            finally:
                response.close()
        raise RepositoryError("Zenodo redirected too many times")


def _origin(url):
    """(scheme, host, port) as urllib3 sends to it, or None for a URL that is
    ambiguous (backslashes, whitespace or control characters) or carries
    credentials."""
    if not url or any(char == "\\" or ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        return None
    try:
        parsed = parse_url(url)
    except ValueError:
        return None
    if parsed.auth or not parsed.scheme or not parsed.host:
        return None
    scheme = parsed.scheme.lower()
    return scheme, parsed.host.lower(), parsed.port or DEFAULT_PORTS.get(scheme)


def _time_left(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise RepositoryError("Zenodo took too long to send the response")
    return left


def _access(record):
    """{"record", "files"} as "public" or not. The legacy shape has only
    metadata.access_right, where "open" means record and files are public; it is
    read only when the RDM `access` block is absent, so a malformed block is
    never open."""
    if "access" in record:
        access = record["access"] if isinstance(record["access"], dict) else {}
        return {"record": access.get("record"), "files": access.get("files")}
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    right = metadata.get("access_right")
    level = "public" if right == "open" else right
    return {"record": level, "files": level}


def _entries(files):
    """Zenodo lists a record's files as a list, or, in its newer records API, as
    entries keyed by name. Any other shape lists none."""
    if isinstance(files, dict):
        entries = files.get("entries")
        files = list(entries.values()) if isinstance(entries, dict) else []
    if not isinstance(files, list):
        return []
    return [entry for entry in files if isinstance(entry, dict)]


def _file(entry, record_url):
    """One file of a record. Fields of the wrong type read as missing, so a
    malformed record is not downloadable rather than an error."""
    links = entry.get("links") if isinstance(entry.get("links"), dict) else {}
    link = _text(links.get("content")) or _text(links.get("download")) or _content_link(_text(links.get("self")))
    size = next((value for value in (entry.get("size"), entry.get("filesize")) if _count(value)), None)
    try:
        url = urljoin(record_url, link) if link else None
    except ValueError:
        url = None
    return {
        "key": _text(entry.get("key")) or _text(entry.get("filename")),
        "size": size,
        "checksum": _text(entry.get("checksum")),
        "url": url,
    }


def _text(value):
    return value if isinstance(value, str) and value else None


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _content_link(link):
    """The legacy shape's `self` file link is the content itself; the RDM one is not."""
    return link if link and link.rstrip("/").endswith("/content") else None


def _read_capped(response, max_bytes, deadline):
    encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding != "identity":
        raise RepositoryError(f"Zenodo sent the response {encoding}-encoded, though it was asked not to")
    try:
        declared = int(response.headers.get("Content-Length", ""))
    except ValueError:
        declared = None  # Missing or malformed: the streaming check below still holds.
    if declared is not None and declared > max_bytes:
        raise FileTooLarge(max_bytes)
    body = bytearray()
    # read1 returns whatever has arrived, so the deadline is checked between
    # small reads, not only after a full chunk.
    while chunk := response.read1(CHUNK_BYTES, decode_content=False):
        if len(body) + len(chunk) > max_bytes:
            raise FileTooLarge(max_bytes)
        _time_left(deadline)
        body.extend(chunk)
    _time_left(deadline)
    return bytes(body)
