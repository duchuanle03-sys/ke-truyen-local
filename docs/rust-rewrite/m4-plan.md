# M4 QT dictionary and translation

The Rust backend continues to serve the existing JavaScript frontend. QT opens lazily inside an unsealed synthetic fixture under the existing exclusive library lock. The Python backend and its databases remain the reference.

- Port `qt.py` into `rust/src/qt.rs` with the existing `qt.sqlite3` schema, restricted rule grammar, scope-first precedence and code point offsets. Build immutable per-scope snapshots from reader connections, retain up to eight private scopes, and publish edits after commit. Keep restore available on the dictionary store.
- Wire dictionaries, import, export, rule preview, translation and the QT capabilities field through existing HTTP guards. Keep the writable startup pack option `--import-qt`, preserve existing entries, and write the installation report inside the synthetic fixture.
- Use synthetic inputs for HTTP differential tests, pack rollback, snapshot consistency and same-machine release measurements. Keep reference suites and the frontend unchanged. Record source behavior with Rust Anything app record and replay.
- Reuse the M3 implementation and verification sessions. Implementation owns `qt.rs`, verification owns the new integration fixtures and measurement harness, and the main session owns HTTP, pack, CLI and documentation. Request independent review after implementation and focused tests.

Measure the 1,014,374-entry profile before the gate. Require exact equivalent outputs, single-edit publication work bounded by the changed path, and successful concurrent reader consistency and rollback checks. Compare cold construction, warmed acquisition, translation, mutation, writer wait, reader tail and memory on the same machine. The targets are at most 1.25× the matched Python timing plus 1 ms for cold construction, warmed operations, mutation and concurrent reader tail, and at most 1.05× Python for RSS and peak memory. Investigate and report any misses before accepting the milestone.
