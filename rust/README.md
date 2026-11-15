# Fixture-only Rust shelf slice

This package serves unchanged static files and `GET`/`HEAD /api/books` on loopback. It requires an explicit synthetic fixture; it never starts the Python application, creates a schema, launches maintenance, or opens a default library.

```powershell
cargo run --manifest-path rust/Cargo.toml -- --fixture C:/temporary-fixture/library.sqlite3 --web-root web --port 0
```

The fixture directory must contain `m1-fixture.json` with `format: "reader-local-rust-m1"`, `version: 1`, `database: "library.sqlite3"`, `synthetic: true`, `unicode: "16.0.0"`, and a Boolean `sealed` field. The database must have the current shelf columns and exact current shelf/group index definitions. Startup writes one flushed JSON line containing the bound `port`. Canonical paths and every existing path component are checked; symlinks and Windows reparse points are rejected. Database connections are read-only and `query_only`, with an authorizer that rejects writes and chapter reads. Active WAL fixtures may require readable/writable SQLite sidecars; failures never fall back to a writable database.

Generate fresh synthetic fixtures and run the differential gate from the repository root:

```powershell
cargo build --release --manifest-path rust/Cargo.toml
python -B -X utf8 rust/tests/support/capture_reference.py --binary rust/target/release/reader_local.exe
```

The harness prints its newly created OS-temporary root; a fixture is available at `<printed-root>/small/library.sqlite3`. It never accepts an existing input library. Omitting `--binary` captures fixtures and deliberately exits nonzero for the missing-implementation gate.

`casefold.rs` contains Python 3.14.7 / Unicode 16 casefold mappings and Python whitespace/printability properties. The SQLite function is registered before preparing shelf statements, preserving persisted expression indexes. Each shelf request counts and selects within one read transaction, retaining the reference's indexed queries and maximum 200 items.

Dependencies are locked in `Cargo.lock`: bundled SQLite provides the required FTS5/JSON/generated-column/trigger behavior; Hyper/Tokio supplies HTTP framing and bounded connections with idle timeouts; serde_json preserves object ordering and accurate float parsing; SHA-256 and Base64 preserve public image URLs; getrandom supplies session-cookie entropy; regex/HTML entity decoding/XML tokenization implement the public-output boundary. No TLS client or source downloader is included.

The independent Python harness creates disposable synthetic fixtures and compares response JSON, static bytes, headers, methods, guards, sealed hashes and WAL snapshots. Rust tests additionally check every Unicode scalar, native SQLite features, no per-row folds on a 50,000-book shelf, authorizer restrictions, and Windows junction escapes.

```powershell
cargo test --manifest-path rust/Cargo.toml
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --all-targets
cargo clippy --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --release --manifest-path rust/Cargo.toml --test shelf_scale -- --nocapture --test-threads=1
```

Tests require the pinned Python reference interpreter; the Rust runtime does not. Fixtures and benchmark output stay in the OS temporary directory. This milestone does not implement bootstrap/settings/sources/groups/reader/cover routes or claim full frontend startup parity. It is not a production launcher.
