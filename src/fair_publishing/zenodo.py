"""Zenodo's deposit API, with the user's own personal access token, and its
public records API, which needs no token."""
import json
import re
from urllib.parse import quote, urljoin

import requests
import urllib3
from urllib3.util import parse_url

TIMEOUT_SECONDS = 120
CHUNK_BYTES = 64 * 1024
MAX_REDIRECTS = 3
RECORD_MAX_BYTES = 10 * 1024 * 1024
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
        url = f"{self.base_url}/api/records/{record_id}"
        try:
            record = json.loads(self._public_get(url, RECORD_HEADERS, RECORD_MAX_BYTES))
        except ValueError as exc:
            raise RepositoryError("Zenodo sent a record that is not JSON") from exc
        access = _access(record)
        files = [_file(entry, self.base_url) for entry in _entries(record.get("files"))]
        files = [entry for entry in files if entry["url"]]
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
        return self._public_get(file["url"], FILE_HEADERS, max_bytes)

    def _public_get(self, url, headers, max_bytes):
        """GET a public URL on the configured Zenodo host and return its body.

        Uses urllib3 rather than requests, on purpose: the host is checked with
        the same parser that sends the request, no .netrc credentials are added,
        and a redirect is not followed or its body read. Each redirect target is
        checked again before it is requested."""
        http = urllib3.PoolManager(retries=False, timeout=urllib3.Timeout(connect=30, read=TIMEOUT_SECONDS))
        for _ in range(MAX_REDIRECTS + 1):
            if _origin(url) is None or _origin(url) != _origin(self.base_url):
                raise RepositoryError("Zenodo linked a file somewhere other than Zenodo, so it was not fetched")
            try:
                response = http.request(
                    "GET", url, headers=headers, redirect=False, preload_content=False, decode_content=False
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
                    url = urljoin(url, location)
                    continue
                if response.status >= 300:
                    raise RepositoryError(f"Zenodo refused the request: {response.status}")
                return _read_capped(response, max_bytes)
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


def _access(record):
    """{"record", "files"} as "public" or not. The legacy shape has only
    metadata.access_right, where "open" means record and files are public."""
    access = record.get("access")
    if isinstance(access, dict):
        return {"record": access.get("record"), "files": access.get("files")}
    right = (record.get("metadata") or {}).get("access_right")
    level = "public" if right == "open" else right
    return {"record": level, "files": level}


def _entries(files):
    """Zenodo lists a record's files as a list, or, in its newer records API, as entries keyed by name."""
    if isinstance(files, dict):
        files = list((files.get("entries") or {}).values())
    return [entry for entry in files or [] if isinstance(entry, dict)]


def _file(entry, base_url):
    links = entry.get("links") or {}
    link = links.get("content") or links.get("download") or _content_link(links.get("self"))
    size = entry.get("size")
    return {
        "key": entry.get("key") or entry.get("filename"),
        "size": size if size is not None else entry.get("filesize"),
        "checksum": entry.get("checksum"),
        # A relative link is on the Zenodo host itself.
        "url": urljoin(f"{base_url}/", link) if link else None,
    }


def _content_link(link):
    """The legacy shape's `self` file link is the content itself; the RDM one is not."""
    return link if link and link.rstrip("/").endswith("/content") else None


def _read_capped(response, max_bytes):
    encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding != "identity":
        raise RepositoryError(f"Zenodo sent the file {encoding}-encoded, though it was asked not to")
    try:
        declared = int(response.headers.get("Content-Length", ""))
    except ValueError:
        declared = None  # Missing or malformed: the streaming check below still holds.
    if declared is not None and declared > max_bytes:
        raise FileTooLarge(max_bytes)
    body = bytearray()
    for chunk in response.stream(CHUNK_BYTES, decode_content=False):
        if len(body) + len(chunk) > max_bytes:
            raise FileTooLarge(max_bytes)
        body.extend(chunk)
    return bytes(body)
