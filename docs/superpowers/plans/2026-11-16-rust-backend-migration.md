# Rust Backend Migration Implementation Plan

> **For agentic workers:** After user approval, use `superpowers:subagent-driven-development` or `superpowers:executing-plans` task by task. The project requires narrowly scoped `rust_implementer` ownership and independent `verifier`/`critical_reviewer` gates. The user subsequently approved M1 only; M2 and later milestones remain unapproved.

**Goal:** Fully replace the Python backend and Node.js STV transport with Rust while preserving the existing JavaScript/HTML/CSS frontend, HTTP API and user data.

**Architecture:** One Rust package under `rust/`, with domain modules and a library/binary entrypoint. Validate against immutable Python/Node references using separate synthetic fixtures; cut over only after complete parity.

**Tech stack:** Windows x86_64 MSVC; Rust stable; M1 selects Hyper/Tokio, serde/serde_json and bundled rusqlite with versions in `rust/Cargo.lock`. Native SQL and dependency checks are recorded in the M1 results.

**Spec:** [ADR 0001](../../rust-rewrite/ADR-0001.md), [behavior contracts](../../rust-rewrite/contracts.md), [observed environment/baselines](../../rust-rewrite/environment.md).

## Global constraints

- JavaScript remains JavaScript. Preserve all `web/` production files byte-for-byte; no Rust/WASM frontend, redesign or changed UX.
- Preserve Python/Node production files and existing tests as reference inputs until full parity and approved cutover. Preserve `Start.cmd` until its replacement is proven.
- Never inspect, mutate or commit real `user-data/`, `library.sqlite3`, `qt.sqlite3`, imported originals, downloaded files, dictionaries, backups, profiles or secrets during preparation/testing.
- Rust implementation lives in `rust/` in the existing isolated `rust-rewrite` worktree; no second worktree is needed.
- One writer per file set, at most three concurrent subagents. Orchestrator approves interfaces and coordinates any changes to shared files.
- Design for hundreds of books and up to millions of chapter records. Indexed/scoped queries, bounded materialization, short write transactions and asset locks; no interactive/startup full-body scans. Ordinary maintenance scales with affected records/assets. One-time upgrades are bounded, durable and restartable.
- Each milestone requires executable contracts, input/output fixtures, same-machine baseline evidence, focused tests, release measurements where relevant, fmt/clippy and independent review. Compilation is not parity.
- No runtime Python or Node dependency at final cutover. Preserve protected data and security checks throughout.
- Before a commit remove only proven superseded code, checking callers/tools/dynamic references/tests; do not delete the immutable reference early. Every local commit must immediately be pushed to the configured remote/branch per AGENTS.md. Preparation makes no commit.

## Review focus

1. Wrong Unicode function results can silently invalidate persisted expression indexes: full scalar casefold differential and exact SQLite feature probes belong to M1, before any M2 writer.
2. Reader GETs can mutate LRU/leases: lifecycle/lock/write-trigger compatibility belongs at the beginning of M2, with GET/kill/restart/eviction tests.
3. Reference startup mutates data and read-only WAL uses sidecars: separate sealed fixtures, mutable oracle copies and WAL concurrency fixtures in M1.
4. Shared assets and ambiguous legacy bodies must survive racing reads/purges/evictions: ownership/revision/lease/crash tests belong to M3.
5. Native STV transport and post-write rollback can block release: early transport feasibility plus M5 wire/cancellation tests; backward-readability and coordinated rollback gates in every writable milestone and M7.

## M0 — preparation: completed before M1 approval

- [x] Read all three requested project instructions and verify branch/worktree.
- [x] Receive parallel read-only `architect` and `parity_auditor` reports.
- [x] After both reports, obtain independent `critical_reviewer` critique.
- [x] Verify rustc/cargo/toolchain and build/run disposable native MSVC hello-world offline.
- [x] Run safe reference HTTP/shelf/cache checks and collect dataset-qualified baselines.
- [x] Save ADR, route ownership, contract checklist, milestone gates, file ownership and first task.

Evidence: **34 reference Python tests passed**, plus four Node syntax checks. Shelf: 50,000 books/3 cached bodies, page60 median22.546ms (five samples). Cache: 100,000 chapters/512 cached bodies, select8 4.373ms/evict8 36.847ms (single observations). These are not accepted Rust budgets. No Rust backend crate exists yet; no private data or production source was modified.

## Route-to-module ownership

These are intended Rust owners of existing route families; exact branch/method fall-through and error behavior must be captured from `server.py:863` rather than inferred from REST conventions. HEAD aliases GET where the reference does. No route is removed or renamed.

- **`http`**: `/`, `/index.html`, `/app.js`, `/reader.js`, `/captcha.js`, `/style.css`; `/api/instance`; guards, request parsing, public serialization, cookies/security headers and errors for every route.
- **`library`**: `/api/books`, `/api/groups`, `/api/settings`; `/api/books/{id}` metadata GET; POST `.../{update,trash,restore}`; DELETE `/api/books/{id}` coordinating assets/jobs. Preserve settings' existing method fall-through as captured, rather than imposing a new method policy.
- **`chapters`**: GET `/api/books/{id}/chapters`, `.../chapters/{index}`; POST `.../progress`, `.../chapters/{index}/{edit,restore}`; POST `/api/readers`; `/api/notes` GET/POST and DELETE `/api/notes/{id}`; GET/POST `/api/cache/chapters` and POST its `clear`, `legacy`, `compact` actions (compaction coordinates maintenance/lifecycle).
- **`assets`**: GET `/api/image`, `/api/books/{id}/cover`; GET/POST `/api/cache/images`, POST `/api/cache/images/clear`; managed-asset indexing, streaming, leases, offline pins and GC. Does not own imported original PDF response policy.
- **`search`**: GET `/api/books/{id}/search`, normalized-doc/FTS queues and bounded backfill/reclaim.
- **`sources`**: GET `/api/sources`, `/api/sources/{source}/{search,detail}`; POST source `add`, chapter `original`; GET `/api/sources/stv/captcha`; POST CAPTCHA `refresh`, `verify`, `cancel`. Native STV sessions and priority/throttle remain one coordinated domain.
- **`downloads`**: POST `/api/books/{id}/download`; GET `/api/jobs`; POST `/api/jobs/{id}/{action}`, durable checkpoints/scheduling/recovery.
- **`qt`**: `/api/dictionaries` GET/POST/DELETE, POST `import`, GET `export`, POST `rules/preview`, POST `/api/qt`; pack/backup/restore tooling. Unpaginated dictionary GET retains its legacy list shape; paginated GET retains its envelope.
- **`documents`**: POST `/api/import/{file,backup}`; GET `/api/books/{id}/file`; safe document parsing/OCR and managed-original integration.
- **`platform`**: GET `/api/capabilities`; POST `/api/books/{id}/shortcut`; Windows PDF/OCR/shortcut helpers and launcher integration.
- **`lifecycle`**: POST `/api/shutdown`; exclusive library lock, admission, resource ownership, cancellation/drain/startup failure. Every service uses this owner; the launcher uses `/api/instance` with exact same-library checks.

Production source boundaries: `server.py`, `library.py`, `chapter_cache.py`, `qt.py`, `qt_pack.py`, `sources.py`, `stv_source.py`, `stv_http.py`, `stv_http_worker.mjs`, `documents.py`, `legacy_formats.py`, `pdfocr.py`, `shortcuts.py`, `lifecycle.py`, `run.py`. Existing HTML/safety helpers must be ported where their contracts require them, not replaced with raw serialization.

## M1 — first vertical slice: fixture-only read-only shelf HTTP

**Contract and interface:** A Rust process accepts only explicit `--fixture PATH --web-root PATH --port PORT`, binds `127.0.0.1` (port0 allowed), serves unchanged static bytes and `GET/HEAD /api/books`. There is no default data directory, schema initialization, maintenance, download worker, source request, browser launch or production launcher integration. Require the harness-generated fixture marker before opening its DB. Missing/unsupported/malformed fixtures fail closed, with no creation or writable fallback.

The first slice intentionally does not implement `/api/settings`, `/api/sources`, `/api/groups`, covers or reader routes. Preserve shelf cover URLs even though those targets arrive later. A direct shelf HTTP contract test and static-byte comparison are its acceptance; **complete frontend startup/render parity is not claimed**. Do not return invented successful placeholders for missing routes.

**Owned files, only after approval:**

- `rust_implementer`: `rust/Cargo.toml`, `rust/Cargo.lock`, `rust/README.md`, `rust/src/{main,lib,http,library,casefold,entities}.rs` and focused Rust unit/integration tests under `rust/tests/*.rs`.
- `verifier`: `rust/tests/support/capture_reference.py`, `rust/tests/support/test_capture_reference.py` and `docs/rust-rewrite/m1-verification.md`. Own independent fixture generation, HTTP assertions and measurements. This ownership follows the explicit M1 execution request; generated expectations, databases and benchmark results remain temporary and untracked.
- Orchestrator: these plan/contract/evidence documents and a narrowly scoped `.gitignore` allowlist for reviewed Rust source/manifests/lockfile/test fixtures and selected Markdown when versioning is requested. Keep build output, benchmark output and synthetic databases ignored. Existing Python/JS/tests/Start.cmd remain read-only inputs.
- Implementer and verifier may read each other's work; neither edits the other's files. Interface changes go through the orchestrator. Do not run performance measurement concurrently with other benchmark jobs.

**Internal seams to freeze before writing:** `Library::open_fixture(path: &Path) -> Result<Library, ApiError>` never initializes a schema; `Library::list_books(&self, query: &ShelfQuery) -> Result<ShelfPage, ApiError>` preserves the shelf envelope; `ShelfQuery::from_uri(uri: &str) -> Result<ShelfQuery, ApiError>` preserves Python normalization; `casefold(text: &str) -> String` matches the pinned reference mapping. `ShelfPage` contains exactly items/total/shelf_total/limit/offset/next_offset. Parse/clamp HTTP integers compatibly through the pinned Python 4300-digit conversion limit before binding bounded SQL values; preserve the conversion-limit error above that limit and do not let an i64 parser reject an otherwise valid clamped value. The HTTP boundary applies recursive safe public output and status/header/error policy.

### Concrete implementation task and test gate

- [x] **1. Pin the reference fixture and dependency contract.** Save Python3.14.7/Unicode16.0.0/SQLite3.50.4 metadata and schema objects in a synthetic fixture manifest. Generate all Unicode scalar casefold expectations (excluding surrogate code points) and compare every scalar, including expansions, against the proposed Rust mapping. Validate exact JSON/generated-column/expression-index/trigger/FTS5 `trigram case_sensitive 1` SQL on the selected Rust SQLite build. Choose/pin dependencies and lockfile only after these checks. A Python-only capability probe is insufficient.
- [x] **2. Capture isolated reference outputs.** The new harness accepts only a fresh output root, creates its own marker and synthetic data, and refuses arbitrary existing DB/private paths. Generate a canonical current-schema fixture using temporary reference initialization, then stop/checkpoint/finalize it before sealing/hash capture. Run Python App only on separate disposable copies. Keep a distinct WAL fixture with a separate synthetic writer; its shared-memory sidecars need not remain byte-identical. No `immutable=1`, no private fixtures and no full-library scan.
- [x] **3. Write independent failing contract tests first.** Test names/assertions: `shelf_matches_python_pages` compares the complete JSON envelope/metadata and all stable ties/filters; `unicode_matches_python` compares all scalar folds and shelf queries; `parser_matches_python` covers blanks/duplicates/plus/invalid-UTF8, `limit=1_0`, `limit=١٠`, invalid kinds/numbers, negatives, oversized valid numbers and overshoot; `shelf_applies_public_output` compares nested cover/data-image/HTML trust-boundary cases and encoded IDs. Use cases in `test_bookshelf.py`, `test_server.py`, `test_safe_output.py` without editing them.
- [x] **4. Add failing HTTP/safety/integrity tests.** `http_matches_reference` checks exact Host/Origin, permitted methods, GET/HEAD Content-Length/body, OPTIONS403 on reused connection, traversal (including Windows paths/reparse escapes), security headers, localized failures, cookies and static byte hashes. Capture other unsupported-method behavior instead of inheriting framework405. `fixture_remains_unchanged` checks all sealed fixture hashes/no writes/migrations/workers/body queries; invalid/missing schema never falls back to production storage. `shelf_snapshot_allows_writer` tests count/page consistency against the separate WAL writer fixture and accepts an explicit read-only failure when safe sidecar access is unavailable.
- [x] **5. Confirm tests fail for missing Rust behavior.** Missing crate/entrypoints or explicit unimplemented results must produce a genuine failing gate; do not prefill expected success from the implementation. Record the failure before implementing only the owned M1 files.
- [x] **6. Implement the minimum shelf service.** Port the indexed scoped shelf SQL and output transformation, registered deterministic UDF and read transaction. Open SQLite read-only/query_only without maintenance; materialize at most one page (maximum200) and no chapter bodies. Serve the existing static tree read-only. Suppress body for HEAD and preserve response semantics. Do not add a proxy to Python or a second writable production runtime.
- [x] **7. Verify focused parity and source integrity.** Run `cargo test --manifest-path rust/Cargo.toml --test sqlite_compat --test http_contract --test shelf_contract`; `cargo fmt --manifest-path rust/Cargo.toml -- --check`; `cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings`. All must exit0. Assert unchanged production/frontend hashes and synthetic fixture safety. After dependency acquisition, ensure tests also work offline from the pinned lockfile/cache.
- [x] **8. Measure the first slice in release mode.** Run `cargo test --release --manifest-path rust/Cargo.toml --test shelf_scale -- --nocapture --test-threads=1`. Reproduce 50,000 books/3 cached bodies/page60. Assert bounded materialization, no chapter-body/table reads or per-row folding on indexed normal pages, expected sort index/no temporary sort, and stable snapshots. Compare equivalent Python/Rust query and HTTP work using the same synthetic corpus and output transformation. Capture at least30 warm samples and three process-cold runs; report warm p50/p95/max, cold samples/median/max, JSON bytes, RSS/peak, DB/WAL bytes, query plans and contention. Do not compare the partial Rust process's startup/RSS as an improvement over a full Python App with extra services.
- [ ] **9. Independent gate, then narrow commit/push.** Proposed same-scope warm latency ceiling is no more than20% above the new Python p50/p95; establish/review those baselines before using it as a gate (the preparation's five-sample median is insufficient). Structural bounded-work assertions are mandatory regardless of timing. Verifier supplies evidence; critical_reviewer clears blockers; orchestrator approves M1 only, then any local commit is immediately pushed to `origin/rust-rewrite`.

## M2 — lifecycle first, then complete offline reader and bootstrap

**Owners:** One implementer owns new `rust/src/lifecycle.rs`, `chapters.rs`, `assets.rs` and necessary edits to `http.rs`/`library.rs`; verifier owns new `rust/tests/lifecycle_contract.rs`, `reader_contract.rs`, `offline_bootstrap.rs`. Shared-file edits are sequential. No real data.

**Contract/I/O:** First implement exclusive `.server.lock` compatible with the reference, bind-before-data-side-effects, admission, cancellation, retryable drain, WAL/foreign-key/trigger/UDF compatibility and backward-readable writes. Then genuine settings/groups/sources bootstrap reads, book detail/TOC/cached chapters/covers/images, reader leases/progress/notes and small metadata/settings writes. Cached chapter/image GETs update metadata, so they are gated on this writable lifecycle foundation. Source catalogue is real reference metadata, without enabling network downloads.

**Tests/baseline/gate:** Differentially port `test_shutdown.py`, `test_shutdown_restart.py`, `test_run.py`, reader access/chapter access/image-streaming and shelf UI contracts. Test Python/Rust cross-process lock exclusion on synthetic directories, cached GET with reader headers followed by kill/restart and eviction protection, stale sequences, blocked reads, failed startup/cleanup and aborting connections. Preserve full book-scoped metadata TOC without body loads. Open synthetic post-Rust databases with the Python reference to prove backward readability. Run unchanged frontend boot/shelf/open/read/progress/notes/browser flows with genuine bootstrap responses. Record startup/first chapter/large TOC/body count/streaming RSS and concurrent writer latency against the equivalent reference service scope; no unreviewed regressions. Focused parity, fmt/clippy and independent review must pass before enabling these routes.

## M3 — durable maintenance, caches, search and destructive operations

**Owners:** Implementer owns `rust/src/search.rs` and sequenced library/chapters/assets/lifecycle edits. Verifier owns `rust/tests/maintenance_contract.rs`, `cache_contract.rs`, `search_contract.rs`, `recovery_scale.rs`.

**Contract/I/O:** Port compatible schema upgrades, edits/original restore, archive/trash/restore/purge, chapter/image budgets, ownership/pins/GC, FTS/backfill/reclaim and explicit compaction. Preserve pending tokens/revisions/generations and durable cursors. Long parsing, file enumeration/deletion and compaction admission checks respect short locks. One-time upgrades may scan incrementally; ordinary operations may not. Never reinterpret downloaded as offline ownership.

**Tests/baseline/gate:** Differential gates are existing cache/image/asset cleanup/search/trash/download-preservation tests. Inject kill/restart at metadata commit, file write/unlink and cursor advance; race editor/fetch/reader/purge/GC and Windows deletion failures; protect shared/original/legacy/offline assets and orphan notes. Reproduce100k chapters/512 bodies and100k image metadata/zero image files, then a new **500-book/1,000,000-chapter** workload declaring body sizes/counts separately (profiles512 and10,000 cached bodies). Measure startup, shelf/TOC/chapter reads, eight-body eviction,32-asset cleanup, import/delete latency, RSS, bytes/I/O and SQLite contention while bounded readers/writers/maintenance run. Count queries/rows/body decodes to prove affected-record work. Baselines are the Python reference on the identical corpus, not historical README numbers. Assert schema/trigger equivalence, crash resumability and backward readability before independent approval.

## M4 — QT dictionary and translation parity

**Owners:** Implementer owns `rust/src/qt.rs` (split only if needed within a separately approved owned set) and sequenced HTTP wiring. Verifier owns `rust/tests/qt_contract.rs`, `qt_snapshots.rs`, `qt_scale.rs` and public synthetic dictionary fixtures.

**Contract/I/O:** Exact dictionary list/page/save/delete/import/export/pack/backup/restore/preview/translate contracts. Scope-before-tier, restricted grammar, immutable chapter snapshots, path-copy edits, global plus eight private scopes, separate read connection, generation/data_version invalidation and postcommit failure handling. Preserve `qt.sqlite3` schema and backups. No experimental compact-index redesign bundled into parity work.

**Tests/baseline/gate:** Differential golden translation/offset/Unicode/grammar cases plus current QT/pack/snapshot adversarial tests. Measure the1,014,374-entry synthetic profile, 10k/100k translation, cold construction/warm acquisition, edit/delete/import/restore, reader tail through publish and old-snapshot reclamation, and writer wait times. Check allocation/RSS/peak and output hashes under concurrent rebuild/external commits. Preserve reader consistency and rollback/readability; no performance claim from a smaller dictionary. Establish same-machine relative budgets before implementation acceptance; review tradeoffs explicitly.

## Early feasibility gate + M5 — public sources, native STV and durable downloads

**Owners:** Implementer owns `rust/src/sources.rs`, `rust/src/downloads.rs` and later a focused `sources/stv.rs`/transport owned set if size warrants it; shared HTTP/library/lifecycle edits stay sequential. Verifier owns `rust/tests/source_contract.rs`, `stv_transport.rs`, `stv_scheduling.rs`, `download_recovery.rs` and public fixture responses.

**Contract/I/O:** Before investing in full source migration, run a narrowly scoped native STV transport feasibility task after M1 approval/dependency checks. This is fixture-only first; it does not enable production networking or replace Node. Establish HTTPS wire/session feasibility before fixing the client choice. Then port public-address/DNS/redirect/signed-URL validation and QQ/Full/Hako adapters, STV memory-only sessions/CAPTCHA/source Trans/identity/error behavior, then durable download scheduling/checkpoints/resume.

**Tests/baseline/gate:** Existing source fixtures and STV/download tests establish exact outputs. Fake-clock tests prove at least10s spacing including bootstrap, interactive priority/FIFO/64-waiter bound, two workers/one STV background job, cancellation while throttled/in verification, no answer replay and no secret persistence. Crash after each checkpoint/state transition, late workers after deletion, CAPTCHA ownership/requeue failures. Measure queue wait and reader latency under concurrent downloads/maintenance with declared corpus/body counts; no network or parsing under database locks. Controlled narrow live STV transport/CAPTCHA acceptance is required before Node retirement, and fixture tests cannot substitute for it. Use REA if shipped/runtime behavior needs it; ordinary source analysis is sufficient for preparation. Retain Node/Python solely as reference until all gates pass.

## M6 — documents, backup import and Windows integration

**Owners:** Implementer owns `rust/src/documents.rs`, `rust/src/platform.rs` and explicitly scheduled library integration. Verifier owns `rust/tests/document_contract.rs`, `import_recovery.rs`, `windows_integration.rs`. Existing Python/PowerShell helpers remain unchanged reference inputs.

**Contract/I/O:** Port supported TXT/EPUB/comic/document/legacy backup formats, input limits, safe output/images, original preservation, OCR/PDF and shortcuts/capabilities. Reuse unchanged PowerShell helpers temporarily only where they have no Python/Node runtime requirement; settle final packaging before M7. Do not add unsupported formats or alter frontend UX.

**Tests/baseline/gate:** Existing documents/legacy/PDFOCR/shortcut tests plus bombs, paths/reparse points, malformed encodings/spines/images, truncated uploads, reordered TOC, reimport tombstones/progress and mid-import crash recovery. Real Windows PDF/OCR installed-language/signature/page-limit tests and shortcut launch/icon fallback are mandatory. Compare representative synthetic file sizes/page counts/import latency/peak memory/disk writes and concurrent reader responsiveness; report corpus details and untested formats. Verify original files/backups remain intact and reference can read Rust writes.

## M7 — complete parity, coordinated cutover and rollback

**Owners:** Orchestrator approves retirement and packaging interfaces. Implementer owns new Rust launcher/packaging files and only then the approved `Start.cmd` replacement; verifier owns complete route/frontend/upgrade/rollback/crash/performance gates; independent critical_reviewer owns final blocker assessment, read-only.

**Contract/I/O:** Every route and every [behavior checkbox](../../rust-rewrite/contracts.md) has evidence; all frontend files remain unchanged. Runtime uses Rust with declared Windows/PowerShell dependencies and no Python/Node. Preserve same-library instance reuse, shortcuts, authentication and safe drain.

**Tests/baseline/gate:** Run full reference/differential suites, release fmt/clippy/tests, unchanged-frontend browser flows, source transport/Windows integrations and500-book/1M-chapter mixed load with512 and10k cached-body profiles, plus the million-entry QT workload. Measure full equivalent-service startup/shelf/reader/TOC/import/download/delete/cache/shutdown, RSS/peak, DB/WAL/I/O, row/body counts and lock wait/reader tail. Require independently reviewed same-machine budgets and concurrency/restart evidence, including steady-state maintenance that scales with affected records.

Prove Python can reopen synthetic libraries after Rust writes while schemas remain compatible. Rehearse migration interruptions and coordinated stopped backup/restore of **both databases, WAL state, managed files and pending cleanup**. A pre-Rust backup does not preserve later user changes; do not promise lossless rollback without backward readability or a tested forward-data recovery path. Never combine old DBs with new WAL/files. Choose the explicit rollback boundary before any incompatible schema change. Only after parity/recovery/performance approval retire superseded Python/Node code, checking CLI tools/dynamic imports/tests/docs as well as frontend callers. Keep user data and private profiles untouched by source cleanup.

## Independent critique disposition

Reviewer verdict: preparation passes **with these corrections incorporated**; implementation/cutover are not approved or verified.

- M2 lifecycle/write compatibility now precedes reader GETs that mutate LRU/leases.
- M1 uses sealed canonical fixtures, separate mutable oracle copies and a distinct WAL concurrency fixture. Sidecars are not incorrectly included in a universal byte-immutability assertion.
- Full Unicode16 scalar folding and exact selected-Rust-SQLite probes now gate M1 acceptance and every later writer.
- Parser cases pin blank/repeated values, plus decoding, replacement decoding and Python integer syntax; unsupported methods are captured separately.
- Public output/sanitization remains in M1, despite partial cover/bootstrap route support.
- Every writable milestone and cutover require backward readability; backup-only rollback is not presented as lossless.
- Early native STV feasibility, checked DNS destination, signed URL encoding, cancellation and precise response-bound wording are explicit.
- Performance budgets remain provisional until matched-scope release measurements; cold samples are not mislabeled p95.

## Current handoff

M1 was explicitly authorized, implemented and independently reviewed/verified PASS. The [M1 results](../../rust-rewrite/m1-report.md) and [independent verification](../../rust-rewrite/m1-verification.md) record contracts, tests, measurements, fixes and limits. Git commit/push status is supplied in the execution handoff. Do not begin M2 without separate authorization. Existing Python/JavaScript production code, frontend assets and private data remain unchanged.

Reviewed Rust source, tests/manifests and selected Markdown are eligible for Git through a narrow allowlist. Generated databases, benchmark output, logs, build artifacts, secrets and private-data directories stay excluded. Existing local agent configuration files are preserved outside the M1 commit. REA was considered; source and synthetic runtime probes sufficed.
