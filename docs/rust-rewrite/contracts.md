# Rust migration behavior checklist

Date: 2026-11-16. Source-inspected by independent `architect` and `parity_auditor`; reviewed independently before plan completion. Unchecked boxes are future Rust parity gates, not claims that the reference implementation is untested. Existing tests are reference inputs and must remain unchanged.

## HTTP, security and JavaScript callers

- [ ] Preserve JavaScript/HTML/CSS frontend bytes and HTTP contract; no Rust/WASM frontend. Callers: [app.js](../../web/app.js), [reader.js](../../web/reader.js), [captcha.js](../../web/captcha.js).
- [ ] Bind only `127.0.0.1`; Host is exactly `127.0.0.1:<actual port>` or `localhost:<port>`. A supplied Origin must be one of the corresponding HTTP origins. Non-GET/HEAD cross-site fetches are rejected. Ordinary APIs do not require shutdown credentials.
- [ ] `/` and `/index.html` set a random per-process `ke_truyen_session` cookie with HttpOnly/SameSite=Strict/Path=/api/. Valid-cookie settings reads return `X-Shutdown-Token`; shutdown also requires matching token and JSON content type.
- [ ] Decode URL paths and query values with reference semantics, including first retained repeated query value and ignored blank query values: `q=&q=STRASSE` selects `STRASSE`, `q=a+b` means `a b`, `%FF` becomes a replacement character. Python integer parsing accepts `limit=1_0` and Arabic-Indic `١٠` as 10. HEAD follows GET with no body and compatible Content-Length. OPTIONS returns JSON 403, including after another request on the same connection. Explicitly inventory other methods instead of accepting framework defaults.
- [ ] Preserve body limits: 128 MiB upload, JSON object at most 2 MiB, finite numbers, no Transfer-Encoding, no truncated/negative length. Preserve connection close/drain behavior for errors and rejected bodies.
- [ ] Preserve error status and JSON: missing 404, validation 400, permission 403, CAPTCHA 409 with `captcha`, VIP/auth 403 with `access`, source network/timeout 503, source codes/http_status and shutdown details, sanitized generic 500. Capture exact localized messages in oracle fixtures.
- [ ] Preserve `public_content` recursion, cover proxy rewriting, safe raster data URLs and HTML/image sanitization. Do not serialize raw stored metadata to bypass this trust boundary. Preserve security headers, JSON no-store and static traversal protections.
- [ ] Image/file responses preserve ETag/Last-Modified, conditional GET/HEAD/304, private caching, Content-Length, at most 64 KiB streamed chunks and lease release on disconnect.

Evidence: [server.py](../../server.py), functions `public_content`, `Handler._guard`, `_reply`, `_body`, `_json`, `_dispatch_admitted`, `_route`, `make_server`. Gates: [test_server.py](../../tests/test_server.py), [test_safe_output.py](../../tests/test_safe_output.py), [test_image_streaming.py](../../tests/test_image_streaming.py), [test_shutdown.py](../../tests/test_shutdown.py).

## SQLite, shelf and search

- [ ] Preserve WAL, foreign keys, all schema objects/functions and per-connection ownership. Probe exact FTS5 `trigram case_sensitive 1`, external-content FTS, JSON functions, generated columns, required SQL and trigger behavior. A version number alone is insufficient.
- [ ] Preserve Python Unicode `str.casefold` behavior for persisted expression indexes and literal substring search. Include expansion (`Straße`), sigma, dotted I, composed/decomposed text and non-BMP cases. Pin/differentially validate the whole applicable Unicode mapping before any writer or index rebuild.
- [ ] Shelf envelope is `{items,total,shelf_total,limit,offset,next_offset}`. Default page 60, clamp limit 1–200, clamp negative offset to zero and overshoot to last page. Counts and page share a read snapshot. Stable order is selected key/name/id in matched direction; unknown sort uses recent. Preserve source/group/kind, archive and trash semantics and literal `%`/`_` queries.
- [ ] `_book` preserves stored metadata types, booleans/nulls, groups, trash expiration and warnings/OCR metadata; apply `public_content` at HTTP output.
- [ ] TOC returns the full book-scoped metadata array without bodies; unchanged JS virtualizes it. Do not introduce incompatible HTTP pagination.
- [ ] Search is literal casefold substring, accents retained: nonempty query at most 500 characters/no NUL, `after` cursor, at most 100 results, `{results,searched,total,indexing,next_index}`. Trigram for at least three folded characters; shorter scans stay within the selected book. Exclude stale/pending documents and preserve snippet offsets across casefold expansion.
- [ ] Search backfill persists cursors, seeds at most 256 owners, normalizes outside writes, publishes at most 8 bodies/about 256 Ki characters with pending-token validation. Preserve atomic FTS triggers and bounded reclaim.
- [ ] Notes intentionally have no book FK; retain orphan notes. Preserve durable jobs, asset references/generations/pins/counters/queues and chapter revisions, not just books/chapters.

Evidence: [library.py](../../library.py), `Store` schema/`db`, `list_books`, `chapters`, `search_content`, `index_search`; [test_bookshelf.py](../../tests/test_bookshelf.py), [test_library.py](../../tests/test_library.py), [test_search_content.py](../../tests/test_search_content.py).

## Protected content, caches and deletion

- [ ] `downloaded=1` means body present, not permission to evict. Preserve regenerable `cache`, retained `protected` and uncertain `legacy`. Imported/local, offline, edited and original restore bodies remain protected, including after restoring an edit.
- [ ] Chapter cache defaults to 100, integer range 0–100,000; ordinary clear retains protected/legacy. Legacy reclassification requires `confirmed is True` and completed inventory.
- [ ] Reader leases: token 1–128 characters, integer sequence 0–2^53, at most 128 sessions, five-minute expiry, durable across restart. Older sequences cannot release newer ownership. Active leases prevent eviction.
- [ ] Eviction rechecks revision/protection/source/readers atomically, at most 8 candidates/about 256 KiB per batch (one oversized body remains atomic). Erase body/search/pending consistently; retain TOC, progress and notes. Late fetches cannot overwrite newer edits.
- [ ] Image budget defaults to 1 GiB, integer 0–64 GiB. Zero still allows online reads/offline downloads. Indexed counters/LRU, separate offline pins, shared ownership and ambiguous legacy protection; no ordinary full scan. Image eviction and GC use bounded batches of at most 32; one-time inventory at most 256 entries with durable progress.
- [ ] Soft delete retains assets; purge queues affected assets in the same transaction, checks indexed shared references/active leases, and deletes files outside long locks/transactions. Retry Windows deletion failures durably. Do not follow symlinks/junctions/reparse points or delete original/unmanaged files.
- [ ] Chapter/image GETs may update LRU/leases: writable runtime and lifecycle gates precede them.

Gates: [chapter_cache.py](../../chapter_cache.py), [test_chapter_cache.py](../../tests/test_chapter_cache.py), [test_chapter_cache_api.py](../../tests/test_chapter_cache_api.py), [test_chapter_cache_scale.py](../../tests/test_chapter_cache_scale.py), [test_image_cache.py](../../tests/test_image_cache.py), [test_asset_cleanup.py](../../tests/test_asset_cleanup.py).

## Downloads and sources/STV

- [ ] Unique active job per book; persist ranges/checkpoints/state/errors, never credentials or CAPTCHA IDs. Recovery respects pause/cancel/trash; interrupted running/verification becomes queued. Late UPDATE cannot resurrect deleted jobs.
- [ ] Two admitted download workers, at most one STV background job. Checkpoint each 10 completed chapters or at least 2 elapsed seconds at a chapter boundary, and immediately on state changes. Complete a chapter only after required body/images are saved. Pause/cancel honors started chapter boundaries.
- [ ] STV interactive/CAPTCHA priority over queued background work, FIFO per class, at most 64 waiters, atomic chapter boundaries. Metadata and reading channels are separate; close wakes both without reviving sessions.
- [ ] At least 10,000 ms between every STV `sajax=readchapter` request, including seed/bootstrap, using a cancellation-aware monotonic schedule. Preserve ordered bootstrap/read/optional source Trans and exact source/book/chapter identity checks.
- [ ] Preserve allowlisted origin/path/method, public-address/redirect validation, memory-only cookie ordering/expiry/preferences, manual redirects, fresh IPv4 verification connection and strict UTF-8. Fresh verification bounds both compressed input and decompressed output; ordinary fetch bounds the delivered body (the reference does not prove a separate compressed-wire bound there). Preserve signed URL encoding, validate DNS answers and connect to the checked address. Native TLS/client wire behavior requires controlled verification before retiring Node.
- [ ] CAPTCHA generation/session ownership, refresh rotates ID, previous ID may cancel but cannot verify; require fetched image and trimmed 4–128-character answer; never automatically replay answers. Persist verified requeue before consuming transient verification state. One job cannot cancel another's challenge.
- [ ] Explicit evidence is required for VIP/auth/CAPTCHA; transport/parse/translation failures remain distinct. Chinese STV uses source Trans and `language:'vi', translation:'source'`, never local QT or invented verification.

Gates: [test_sources.py](../../tests/test_sources.py), [test_downloads.py](../../tests/test_downloads.py), [test_download_recovery.py](../../tests/test_download_recovery.py), [test_download_scheduling.py](../../tests/test_download_scheduling.py), [test_stv_scheduling.py](../../tests/test_stv_scheduling.py), [test_stv_transport.py](../../tests/test_stv_transport.py), [test_stv_http.py](../../tests/test_stv_http.py), [test_stv_api.py](../../tests/test_stv_api.py).

## QT

- [ ] Private scope before global; within each scope name > vietphrase > pronouns > hanviet, longest within winning tier. Rules first, private before global, longest in selected scope. Translation uses first `¦` meaning; storage/export keeps all alternatives and `=` characters.
- [ ] Preserve restricted rule grammar, kind aliases/scope validation and ambiguous-delimiter rejection. Missing dictionary errors explicitly for CJK; non-CJK remains readable. Translation limit 2,000,000 characters; preview 100,000.
- [ ] Immutable snapshots for global plus at most eight private scopes; path-copy single edits. A chapter retains acquired snapshots to completion. Warm readers continue while bulk rebuild runs on a separate connection outside pointer lock; generation/data_version prevent stale publication. Failed transaction keeps old snapshot; failed postcommit build invalidates cache.
- [ ] Separate `qt.sqlite3`, WAL, keys/backups, merge/missing/replace import modes. Invalid/empty replace cannot clear data; replace backup and mutation are atomic. Export/import preserves Unicode and scope. Preserve full rebuild/import behavior before optimizing representation; experimental index candidates are not production contracts.

Gates: [qt.py](../../qt.py), [test_qt.py](../../tests/test_qt.py), [test_qt_snapshots.py](../../tests/test_qt_snapshots.py), [test_qt_pack.py](../../tests/test_qt_pack.py), [test_qt_scale.py](../../tests/test_qt_scale.py).

## Lifecycle, reader and documents/Windows

- [ ] Exclusive `.server.lock` persists through final drain. Shutdown refuses named critical work; active downloads require explicit pause consent. Persist pause intent for every DB job before accepted response and admission closure. Flush response before server stop.
- [ ] Abort stalled inbound/upstream work; bounded retryable drain never closes QT/storage, checkpoints or releases lock beneath live work. Failed cleanup is failure and retains resources for retry. Failed startup and launcher reuse preserve ownership.
- [ ] Progress requires finite position 0–1 and valid index; fraction `(index+position)/chapter_count`. Confirmed unreadable uncached VIP preserves prior progress. Preserve reader ID/sequence protocol. Edit/restore only loaded text, retain original once, escape edits into inert paragraphs.
- [ ] Imports: upload 128 MiB, member 64 MiB, expanded archive 256 MiB, 10,000 members, compression ratio 200, generated character budget 256 Mi. Reject traversal/symlink/bomb/active markup and image/spine amplification. Retain source originals, ordering and repair warnings; failure cannot replace a good original.
- [ ] Backup reimport preserves newer progress/content/removal tombstones. Reject shorter/reordered TOC rather than detaching notes/edits/positions.
- [ ] Real Windows PDF/OCR capabilities, installed-language errors, one-based source pages and empty-page warnings. At most 500 pages, dimension 4000, 16M pixels; verify PDF signature. Preserve Windows shortcut quoting/icon fallback and actual-port/same-library instance checks.

Gates: [test_shutdown.py](../../tests/test_shutdown.py), [test_shutdown_restart.py](../../tests/test_shutdown_restart.py), [test_reader_access.py](../../tests/test_reader_access.py), [test_documents.py](../../tests/test_documents.py), [test_pdfocr.py](../../tests/test_pdfocr.py), [test_legacy_formats.py](../../tests/test_legacy_formats.py), [test_run.py](../../tests/test_run.py), [test_shortcuts.py](../../tests/test_shortcuts.py).

## Unverified limits

The preparation ran only the focused tests recorded in [environment evidence](environment.md). No Rust route exists yet. Full suite, million-chapter mixed load, one-million-entry QT, direct STV, browser flows, OCR integration and packaged launcher remain future gates. Do not infer parity or production-scale performance from compilation or source inspection.
