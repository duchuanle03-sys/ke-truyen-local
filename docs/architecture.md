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
- **Next-chapter prefetch** (`prefetch.rs`). At 80% of chapter N the reader asks once for N+1.
  The server runs it on that request's thread: one prefetch at a time and one waiting, for the
  whole app. It skips chapters already saved or in use, and anything but the chapter after the
  one last opened. Sáng Tác Việt prefetches use the background lane and are promoted when the
  reader opens that chapter. Other sources wait 2 s between prefetches. The result is an
  ordinary evictable cache entry; failures are silent and never retried.
  After the chapter is saved, its first 10 images are fetched as ordinary image-cache entries.
  They are found in the saved content (manga pages or embedded illustrations), and each is
  spaced 2 s apart. Opening the chapter never waits for this phase; it stops the rest.
- **Paths.** Canonical paths are checked component by component; symlinks and reparse points are
  rejected for data and static files.
- **`archived=1` means "not on the shelf".**
  - Khám phá reads and downloads create records with `archived=1`; "Thêm vào kệ" sets 0, keeps
    progress and `read_at`, and sets `added_at`.
  - Opening a shelf novel from Khám phá never takes it off the shelf, and a vBook backup import
    puts matching records on the shelf.
  - Records are found again by source plus `key(path)`, so host, query, `.html` and a trailing
    slash don't matter (`find_source_book`).
  - Chapters are identified by position. When the source reorders or drops chapters, the stored list
    is refused, except on a record the clean-up rule says holds nothing (`replaceable`): its rows
    are deleted and rewritten under the clean-up's lock order, which drops their cache, search and
    asset rows, and progress follows its chapter by address (or starts over if it's gone).
- **Only user actions write `read_at` and `updated_at`.** That means progress saves, Khám phá
  `add` and backup import. Downloads, prefetch and maintenance never do. A background job that
  needs to protect a book uses presence or leases, not these timestamps.
- **Lịch sử and the off-shelf clean-up are separate.**
  - History shows off-shelf books read in the last 30 days.
  - The maintenance step (`purge_expired_book`, after the Trash rule) deletes one off-shelf record
    per tick, only when it is untouched for 31 days and holds nothing saved.
  - Saved content is checked on `chapters` itself: downloaded but not plain cache, or edited. Notes,
    the book's own QT dictionary entries, an active job, an open reader, a held chapter lease, groups
    and non-web sources also keep the record. The QT database is read only when an off-shelf record
    is due, with one primary-key seek per (kind, scope) pair.
  - The re-check and delete run in one IMMEDIATE transaction with the `chapter_leases` mutex held
    until commit, the same order `evict_chapters` uses. A lease taken meanwhile waits, then finds
    the book gone.
  - The asset GC removes images and covers only when nothing else references or leases them.
- **Deleting a novel's downloads** (`clear_book_downloads`) empties its web, unedited downloaded
  chapters through the same body-clearing step as cache eviction.
  - Image files are deleted later by the asset GC.
  - Text frees SQLite pages for reuse; the file shrinks only with "Thu gọn".
  - The reply reports freed, pending-file and SQLite-reusable bytes separately.
- **Shutdown ends with a bounded checkpoint.**
  - `final_checkpoint` tries `wal_checkpoint(TRUNCATE)` at most 5 times within 8 s.
  - `/api/shutdown/wait` sends its headers at once, and the window asks for the shutdown only after
    they arrive, so the answer can't be lost when the listener closes. The body then says
    `complete` or `incomplete`, and the window closes only on `complete`.
  - A request still running after 30 s is reported as `busy` and then waited for, without a limit
    and without killing it: the runtime can't exit before blocking work ends anyway. The checkpoint
    runs after it, and the library lock is released last. The window stays open and says so.
  - `incomplete` is not data loss: committed transactions stay in the WAL and are applied at the
    next open.

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
