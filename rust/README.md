# Fixture-only Rust offline reader

This package serves unchanged static files and a bounded shelf on loopback. Explicit `--writable` enables the M3 offline reader and maintenance against an unsealed synthetic fixture. All runtime operations run in Rust using the selected fixture. Source fetching, imports, and the download scheduler remain future milestones.

```powershell
cargo run --manifest-path rust/Cargo.toml -- --fixture C:/temporary-fixture/library.sqlite3 --web-root web --port 0
```

The fixture directory must contain `m1-fixture.json` with `format: "reader-local-rust-m1"`, `version: 1`, `database: "library.sqlite3"`, `synthetic: true`, `unicode: "16.0.0"`, and a Boolean `sealed` field. The database must have the current shelf columns and exact current shelf/group index definitions. Startup writes one flushed JSON line containing the bound `port`. Canonical paths and every existing path component are checked; symlinks and Windows reparse points are rejected. Database connections are read-only and `query_only`, with an authorizer that rejects writes and chapter reads. Active WAL fixtures may require readable/writable SQLite sidecars; failures never fall back to a writable database.

Writable mode requires `sealed: false` and a recognized Python SQLite schema. The listener binds before library side effects. The Python-compatible exclusive `.server.lock` is acquired before SQLite access. Startup verifies the catalog and upgrades supported legacy forms transactionally. These include missing trash and shelf schema, missing search tables, missing asset ownership tables, and older asset inventory and garbage-collection columns. Unsupported catalogs fail before logical writes. SQLite owns its coordination files. Missing, sealed and invalid markers are rejected before locking or opening SQLite.

```powershell
cargo run --manifest-path rust/Cargo.toml -- --fixture C:/temporary-unsealed-fixture --web-root web --port 0 --writable
```

The offline reader serves settings, groups, the source catalogue, instance details, book details and complete scoped chapter metadata, cached chapter/image/cover reads, progress, persisted reader leases, notes and book updates. Images stream in chunks of at most 64 KiB and support ETag, Last-Modified and conditional GET/HEAD.

M3 adds literal casefolded book search, chapter edit and original restore, archive and trash operations, chapter and image cache controls, and confirmed SQLite compaction. Writable fixtures run background maintenance for bounded search and cache backfill, eviction, staged asset cleanup and expired trash. Expired books can remain briefly visible after startup while cleanup processes one book per pass. Restore still checks the 30-day deadline. Compaction requires an idle library, sufficient disk space and paused download jobs.

JSON accepts at most 984 nested object/array containers, including the root; a settings object containing 983 nested arrays is within the limit. Requests exceeding it return 400 with `Dữ liệu JSON lồng quá sâu.` before writing. The same bound applies when reading stored metadata. This is an approved M2 compatibility exception: Python can commit deeper settings before failing to format its response.

`POST /api/shutdown` requires a current UI cookie, matching `X-Shutdown-Token` obtained from authenticated settings, JSON content type and body, and same-origin guards. Active durable download jobs require explicit `pause_downloads: true`. Queued, running and verification jobs persist paused or cancelled intent. The listener closes once shutdown intent has committed and the acknowledgement connection has closed. Connections receive a bounded grace period, stalled connections are cancelled, and blocking SQLite and file work retains ownership until completion. Cleanup joins maintenance and retries its drain and checkpoint before releasing the library lock.

Generate fresh synthetic fixtures and run the differential gate from the repository root:

```powershell
cargo build --release --manifest-path rust/Cargo.toml
python -B -X utf8 rust/tests/support/capture_reference.py --binary rust/target/release/reader_local.exe
```

The harness prints its newly created OS-temporary root; a fixture is available at `<printed-root>/small/library.sqlite3`. It never accepts an existing input library. Omitting `--binary` captures fixtures and deliberately exits nonzero for the missing-implementation gate.

After building the release binary, run the M3 differential gates and the scale workload separately. Timing should run without other tests or builds.

```powershell
cargo test --manifest-path rust/Cargo.toml --release --test search_contract --test maintenance_contract --test cache_contract --test recovery_scale
python -B -X utf8 rust/tests/support/m3_scale.py --binary rust/target/release/reader_local.exe
```

`casefold.rs` contains Python 3.14.7 / Unicode 16 casefold mappings and Python whitespace/printability properties. The SQLite function is registered before preparing shelf statements, preserving persisted expression indexes. Each shelf request counts and selects within one read transaction, retaining the reference's indexed queries and maximum 200 items.

Dependencies are locked in `Cargo.lock`: bundled SQLite provides the required FTS5/JSON/generated-column/trigger behavior; Hyper/Tokio supplies HTTP framing and bounded connections with idle timeouts; serde_json preserves object ordering and accurate float parsing; SHA-256 and Base64 preserve public image URLs; getrandom supplies session-cookie entropy; regex/HTML entity decoding/XML tokenization implement the public-output boundary; httpdate handles HTTP dates. The Windows-only platform module uses windows-sys for opened-handle 128-bit file identity and metadata change time, which stable Rust does not expose. Its small unsafe API boundary is isolated; HTTP/store callers use safe Rust. No TLS client or source downloader is included.

The independent Python harness creates disposable synthetic fixtures and compares response JSON, static bytes, headers, methods, guards, sealed hashes and WAL snapshots. Rust tests additionally check every Unicode scalar, native SQLite features, no per-row folds on a 50,000-book shelf, authorizer restrictions, and Windows junction escapes.

```powershell
cargo test --manifest-path rust/Cargo.toml
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --all-targets
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --release --manifest-path rust/Cargo.toml --test shelf_scale -- --nocapture --test-threads=1
```

Tests require the pinned Python reference interpreter; the Rust runtime does not. Fixtures and benchmark output stay in the OS temporary directory. Full application parity and production launcher replacement remain later milestones.
