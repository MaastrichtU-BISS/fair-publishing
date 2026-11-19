"""Zenodo's deposit API, with the user's own personal access token, and its
public records API, which needs no token."""
from urllib.parse import quote, urljoin, urlsplit

import requests

TIMEOUT_SECONDS = 120
CHUNK_BYTES = 64 * 1024
MAX_REDIRECTS = 3
GONE_STATUSES = (404, 410)
REDIRECT_STATUSES = (301, 302, 303, 307, 308)


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
        or has no files, is not downloadable and lists none. Raises
        RecordNotDownloadable when Zenodo no longer has the record.
        """
        try:
            response = requests.get(f"{self.base_url}/api/records/{record_id}", timeout=TIMEOUT_SECONDS)
            if response.status_code in GONE_STATUSES:
                raise RecordNotDownloadable()
            record = self._check(response, "read the record")
        except requests.RequestException as exc:
            raise RepositoryError(f"Zenodo did not answer: {type(exc).__name__}") from exc
        access = record.get("access") or {}
        files = [_file(entry) for entry in _entries(record.get("files"))]
        open_to_all = access.get("record") == "public" and access.get("files") == "public"
        downloadable = open_to_all and bool(files)
        return {
            "record_id": str(record.get("id", record_id)),
            "access": {"record": access.get("record"), "files": access.get("files")},
            "downloadable": downloadable,
            "files": files if downloadable else [],
        }

    def download(self, record_id, key, max_bytes):
        """The bytes of one public file of an open record, at most max_bytes long."""
        record = self.record_files(record_id)
        if not record["downloadable"]:
            raise RecordNotDownloadable()
        file = next((entry for entry in record["files"] if entry["key"] == key), None)
        if file is None or not file["url"]:
            raise RecordNotDownloadable()
        if file["size"] is not None and file["size"] > max_bytes:
            raise FileTooLarge(max_bytes)
        return self._stream(file["url"], max_bytes)

    def _same_host(self, url):
        """The fetch allowlist: only the configured Zenodo host, over its own scheme."""
        base, link = urlsplit(self.base_url), urlsplit(url)
        return (link.scheme, link.hostname, link.port) == (base.scheme, base.hostname, base.port)

    def _stream(self, url, max_bytes):
        """Fetch a file without a token, following redirects only to the same host."""
        for _ in range(MAX_REDIRECTS + 1):
            if not self._same_host(url):
                raise RepositoryError("Zenodo linked a file somewhere other than Zenodo, so it was not fetched")
            try:
                response = requests.get(url, stream=True, allow_redirects=False, timeout=TIMEOUT_SECONDS)
                try:
                    if response.status_code in GONE_STATUSES:
                        raise RecordNotDownloadable()
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise RepositoryError("Zenodo redirected the file without saying where")
                        url = urljoin(url, location)
                        continue
                    if response.status_code >= 300:
                        raise RepositoryError(f"Zenodo refused to send the file: {response.status_code}")
                    return _read_capped(response, max_bytes)
                finally:
                    response.close()
            except requests.RequestException as exc:
                raise RepositoryError(f"Zenodo did not answer: {type(exc).__name__}") from exc
        raise RepositoryError("Zenodo redirected the file too many times")


def _entries(files):
    """Zenodo lists a record's files as a list, or, in its newer records API, as entries keyed by name."""
    if isinstance(files, dict):
        files = list((files.get("entries") or {}).values())
    return files or []


def _file(entry):
    links = entry.get("links") or {}
    return {
        "key": entry.get("key") or entry.get("filename"),
        "size": entry.get("size", entry.get("filesize")),
        "checksum": entry.get("checksum"),
        "url": links.get("content") or links.get("download"),
    }


def _read_capped(response, max_bytes):
    declared = response.headers.get("Content-Length")
    if declared is not None and int(declared) > max_bytes:
        raise FileTooLarge(max_bytes)
    body = bytearray()
    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise FileTooLarge(max_bytes)
    return bytes(body)
