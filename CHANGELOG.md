# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the version is 0.x, minor releases may contain breaking changes.

## [Unreleased]

### Upgrading

- **Run `fp_0002_publication_creators` (or a host migration that adds the same
  column) before deploying 0.2.0.** `Publication` now has a `creators` column, and
  every registry query selects it, so an unmigrated database fails with "no such
  column". `create_all` does not add columns to an existing table.

### Added

- `Zenodo.record_files(record_id)` and `Zenodo.download(record_id, key, max_bytes)`:
  token-free reads of open records and their public files. They read both
  Zenodo's InvenioRDM and legacy record shapes.
  - Only the configured Zenodo host is requested, for the record and the file,
    redirects included. Each redirect is checked before it is followed, and its
    body is not read. URLs with backslashes, whitespace or credentials are refused.
  - Record ids must be numeric.
  - Each request (the record read, the file fetch) has a 10-minute overall
    deadline, redirects included; connect and read timeouts are capped at the
    time left.
  - A malformed record or file entry reads as not downloadable or raises
    `RepositoryError`, never a bare Python error. Relative file links resolve
    against the record as served, and links off Zenodo are left out of the listing.
  - These reads use urllib3 directly, so no `.netrc` credentials are sent, and
    they ask for unencoded bodies, so `max_bytes` counts the bytes that arrive.
  - `RecordNotDownloadable` ("This record is no longer downloadable") covers a
    record or file that is gone (404/410) or no longer public (401/403),
    restricted and metadata-only records, and a missing file.
  - `FileTooLarge` covers `max_bytes`. It is checked on the declared size and
    while streaming.
- `open_datasets(session)`: the open dataset Publications with a DOI and a Zenodo
  record id, for a host's Community source.
- The registry gets a `creators` column (revision `fp_0002_publication_creators`).
  Entries published before this version list no creators.
- `urllib3>=2.2` is now a direct dependency (`HTTPResponse.read1` arrived in 2.2).

### Changed

- `Zenodo(base_url, token=None)`: the token is optional, and sent only when given.
- Public reads connect directly and trust the system CA store. Proxy and CA
  settings for requests (`HTTPS_PROXY`, `REQUESTS_CA_BUNDLE`) apply to deposits only.

## [0.1.0] - 2026-11-06

### Added

- `init_publishing(app, ...)`: publishing datasets and models to Zenodo under the
  user's own account, through a host-supplied artefact provider and secret store,
  with a host feature gate (on by default), and the HTTP API in
  `docs/openapi.yaml`: config, a write-only token, the user's artefacts, a card
  draft (template, or the host's drafter), preview, publish and the registry.
- Publishing requires the license, rights and personal data attestation, the
  host's required fields, an approved card, the training Dataset's DOI for a
  Model, and a passing round trip for a Model. Open, restricted and
  metadata-only access.
- Each deposit carries DataCite metadata, RO-Crate metadata, the card, legal
  fields and label provenance.
- The `publications` registry table, with an Alembic revision for hosts.
- Repository set up with its FAIR4RS metadata (CITATION.cff, codemeta.json,
  .zenodo.json), license, contributing guide and CI.

[Unreleased]: https://github.com/shashankmc/fair-publishing/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/shashankmc/fair-publishing/releases/tag/v0.1.0
