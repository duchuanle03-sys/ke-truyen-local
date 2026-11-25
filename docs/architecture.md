# Architecture

Kệ Truyện Local is one Rust binary (`rust/`, crate `reader_local`) that serves the unchanged
JavaScript frontend in `web/` on `127.0.0.1` and stores everything in SQLite under `user-data/`.
It replaced a Python backend plus a Node.js STV transport; the last commit containing the Python
app is `491c059`.

## Layout

| Area | Modules |
|---|---|
| Startup | `main.rs` (GUI subsystem, no console), `launcher.rs` (arguments, instance reuse, retries, `reader.log`, app window), `lifecycle.rs` (`.server.lock`, controlled shutdown) |
| HTTP | `http.rs`: Hyper/Tokio server, routes, loopback cookie and shutdown token, HTML sanitizer and public-URL trust boundary, static files |
| Library | `library.rs` (connection pool, shelf queries), `schema.rs` (catalog creation and recognised legacy upgrades), `reader.rs`, `chapters.rs`, `chapter_cache.rs`, `assets.rs`, `search.rs` |
| Dictionary | `qt.rs` (QT dictionary and translation, `qt.sqlite3`), `qt_pack.rs` (pack import) |
| Sources | `sources.rs`, `source_fetch.rs` (QQ, Truyện Full, Hako), `stv.rs` and `stv/` (Sáng Tác Việt over native HTTP with CAPTCHA), `downloads.rs` (durable download jobs) |
| Imports | `imports.rs`, `documents.rs`, `legacy_formats.rs`, `codepages.rs` |
| Windows | `platform.rs` with `windows/pdfocr.ps1` (PDF OCR) and `windows/shortcut.ps1` (Desktop shortcuts), `windows_metadata.rs` |
| Text rules | `casefold.rs`, `entities.rs`: Unicode 16 casefold and HTML5 entity tables |

## Invariants

- **One owner per library.** `.server.lock` in the data folder is held for the server's lifetime.
  A second launch for the same library reuses the running instance (checked through
  `/api/instance`). It never opens the database a second time.
- **The catalog is checked, never guessed.** On open, the SQL catalog's SHA-256 signature must be
  the current one or a recognised older form, which is upgraded in one transaction. Anything else
  is refused before any write. A new library gets the exact catalog in `schema.rs`.
- **`shelf_fold` is registered on every connection before any statement runs.** The shelf's
  expression indexes depend on it (Unicode casefold).
- **SQLite is bounded.** WAL mode, an 8-connection pool whose connections are reset before reuse,
  `journal_size_limit` of 64 MiB, and a `wal_checkpoint(TRUNCATE)` attempt every 30 s while idle.
- **Large-library rules.** Interactive requests use indexed, paginated queries (at most 200 books
  per page), never load or parse every chapter body, and keep write transactions short. Background
  maintenance processes bounded batches.
- **Trust boundaries.** Stored HTML passes through `public_content` (allow-listed tags, proxied
  public image URLs, no private, loopback or reserved hosts). JSON nesting is limited to 984
  levels. Shutdown needs the per-start cookie, a matching `X-Shutdown-Token`, and Host/Origin
  checks. Sáng Tác Việt requests run one at a time, at least 10 s apart, and reader requests go
  ahead of background downloads. QQ, Truyện Full and Hako have no request spacing; only downloads
  are limited, to 2 workers.
- **Paths.** Canonical paths are checked component by component; symlinks and reparse points are
  rejected for data and static files.

## Tests

`cargo test` runs the unit tests in `src/` and the integration tests in `rust/tests/`.
`tests/fixtures/` holds two frozen synthetic libraries (`small`, `empty`) and
`python_reference.json`, the retired Python app's answers for the sanitizer, URL trust boundary,
casefold table, HTML entities, JSON error messages and a shelf page. The `--fixture` command-line
mode serves a fixture read-only for the HTTP tests.

## Known issues

- After a heavy burst of reads and writes, the WAL file can stay at 20–90 MB until the app
  restarts. Something in the process still holds a read snapshot, so the idle checkpoint cannot
  truncate the file. The data is safe; only the disk space is temporarily held.
- `Sources::open` still reads an optional `m5-sources.json` test script from the data folder,
  which is a hook left from the migration harness.
- Small launcher issues:
  - the first-run lock is released and re-taken before opening;
  - the `--import-backup` success message uses the error icon;
  - arguments that are not valid Unicode end the process without a message.
- Windows Defender rescans a file the first time it is opened after a change (about 18 ms). This
  is why the connection pool keeps SQLite connections open instead of opening one per request.
