# M4 QT dictionary and translation

## Scope and data safety

The native store keeps the Python `qt.sqlite3` schema and the existing HTTP dictionary, import, export, rule preview and translation endpoints. The capabilities response exposes the QT status used by the settings page. The frontend and Python reference are byte-for-byte unchanged. The Rust server requires a writable synthetic fixture, opens QT on demand under the library lock, and rejects existing QT database and sidecar paths that resolve through links or reparse points. All test archives and databases were generated for this milestone. Dictionary backup restore uses the store entry point. Pack installation uses the startup option used by the reference and reports inserted, skipped, rejected and pending entries.

The engine preserves private scope before global, tier order, longest match within the selected tier, rule priority and Unicode code point offsets. Single edits path-copy the affected trie. Global and up to eight private snapshots remain immutable while held by a reader. Bulk builds use a separate read transaction and publication rechecks generation and SQLite `data_version`. Replace and its backup commit together, and an unsuccessful post-commit rebuild invalidates the affected cache.

## Independent review

- Fixed integer pagination overflow and retained the order of validation errors. Added differential positive and negative overflow cases.
- Fixed valid-rule preview for numeric scope IDs using SQLite's text conversion. Oversized integers and nonempty unsupported containers keep the reference errors, and a legal JSON numeric exponent overflowing to infinity can resolve the `Inf` scope. Invalid rules return their normal preview before scope evaluation.
- Required consistent local and central ZIP headers for consumed members. A synthetic mismatched required member must fail before readiness and preserve the previous install report and every dictionary row.
- Scoped the dictionary constructor to the library owner and added a test for a hard link to a separate synthetic file. The test verifies that the file stays byte-identical.

## Verification

The locked offline release build produced `reader_local.exe` with SHA-256 `0bd33e36fa74e0e7b796b6de6203e0f9ffd819970bb8a8115432ad9f68c9c522`. From `rust/`, `cargo fmt --all -- --check`, `cargo check --locked --offline --all-targets` and `cargo clippy --locked --offline --all-targets --all-features -- -D warnings` passed.

- `cargo test --locked --offline --no-fail-fast` passed all 70 tests. The scale driver was the single ignored test in this run.
- `cargo test --locked --offline --release --test qt_contract --test qt_snapshots --test qt_scale` passed the two release differential suites. The million-entry driver is opt-in and runs separately. The suites compare JSON, QT status, response status, security headers, SQLite integrity, snapshot consistency and pack failure behavior. A separate synthetic CLI check matched Python install reports for ZIP stored, bzip2, LZMA and Zstandard.
- The unchanged Python QT, snapshot, pack and scale suites passed 36 tests with one optional dependency test skipped. All three tracked JavaScript files passed `node --check`.
- Rust Anything 1.5.1 analyzed the Python reference, recorded 12 synthetic HTTP cases and replayed 12/12 against Python and the final Rust release. The M3 control matched 0/12. The capture covered lists, search and pagination, HEAD with no body, export, scoped Unicode translation, preview offsets and errors. No normalization was applied. Raw capture is in the temporary directory `reader-m4-ra-e3899aacec084a5ea10f5e7a33070c5b`.
- An initial full debug run failed an M2 reader lease timing assertion on the Python side while a separate release build was running. The isolated rerun passed all three M2 tests with the original assertions. The original failure is recorded in `reader-m1-u6mrqzqe/report.json`.

## Million-entry benchmark

One serial release comparison on Windows 11 build 26300 with 12 logical CPUs used Python 3.14.7 with SQLite 3.50.4 and Rust 1.100.0 with bundled SQLite 3.51.1. The raw `python` field holds measurements, so the verifier checked the Python version separately. The synthetic database held 1,014,374 entries in 97,402,880 bytes, with SHA-256 `557e8f71d77f684076275785a481cfc1f5be659e15658f7ce83a20b641f346d3`. Both engines started from identical database bytes. All ten translation output hashes, old and new publication output hashes, mutation reports, final state and SQLite integrity checks matched. The final database retained two Name backups.

Numbers below are Python → Rust. Translation uses 15 samples per workload, warmed acquisition uses 100, and single edits use nine per operation. Cold build, import, replacement, restore and external rebuild are individual observations.

- Cold snapshot build, 6,275 → 4,403 ms. Warm acquisition p50, 6.45 → 2.10 µs.
- 10,000-character translation, 6.42–10.38 → 0.49–0.70 ms p50 across five workloads. For 100,000 characters, 54.06–97.86 → 5.26–7.09 ms p50. Mixed 100,000-character p95, 122.88 → 6.02 ms.
- Global add, update and delete p50, 0.802/0.839/0.815 → 0.762/0.779/0.827 ms. Private p50, 0.805/0.749/0.725 → 0.726/0.501/0.561 ms. Global delete p50 was slightly higher in Rust and within budget.
- Bulk merge with 2,000 inserts and one update, 10,285 → 4,408 ms. Replace, 8,223 → 6,125 ms. Restore of 185,771 names, 8,148 → 4,738 ms. Rebuild after an external commit, 6,896 → 4,052 ms.
- Concurrent reader p95 during publication, 3.246 → 0.423 ms, with 523 Python and 1,719 Rust observations. The second writer took 8,822 → 4,417 ms including its edit. A retained snapshot kept returning the old output while new readers acquired the new output.
- Working set after cold build, 556 → 320 MiB. While old and new snapshots were held, 1,149 → 628 MiB. After releasing the old snapshot, 634 → 331 MiB. Process peak working set, 1,168 → 628 MiB. Peak private bytes, 1,161 → 642 MiB.

All timing and memory targets in the M4 plan were met. These runs measured the direct engines using a warm filesystem. HTTP startup and latency, full production dictionaries and allocator counts are outside these measurements. Native tests separately check that old snapshots are reclaimed. Raw evidence is `reader-m1-epyx1j2n/qt-scale.json` in the OS temporary directory, SHA-256 `a8d30eea45e28354d0d943ee264cab64b3b894f55fc50c32bc9b3da9e7757532`. The optimized driver SHA-256 is `bb3430f024e5dbee330e06bb473f7b451bfc00a2f00615655f57d0f0f950c6f9`. The verifier recorded the frozen 26-file fingerprint `649a55e4bce133defcd5090565ab534df569cd435b914ff64ef41f7a19b152b8`.

## Compatibility limits

Normal frontend preview supplies string book IDs. For direct numeric callers Rust resolves each ID by its SQLite text value. Python can reuse the wrong scope if equal integer and float keys such as `1` and `1.0` warmed the same LRU slot. Pack validation is stricter about inconsistent ZIP headers, bounds the LZMA dictionary at 64 MiB, and reports an equivalent native diagnostic for malformed headers. `Rule.dic` remains pending as in the Python reference. The capabilities evidence covers the QT fields. Later milestones own document, OCR and source capability integration.

## Handoff

- `qt.rs` owns the SQLite dictionary store, rule engine and immutable translation snapshots. `qt_pack.rs` owns pack decoding, validation and startup installation. `library.rs`, `windows_metadata.rs`, `http.rs`, `main.rs` and `lib.rs` integrate ownership, guards and routing. Cargo pins the ZIP reader and supported codecs.
- `qt_contract.rs`, `qt_snapshots.rs`, `qt_scale.rs` and the `m4_*.py` support scripts own differential and scale fixtures. The Git allowlist admits the M4 plan and report. Generated archives and evidence, databases, build outputs and private files remain ignored.
- M4 reused the M3 implementer `01a2057d-21cf-7022-9b62-c9c5302d012e` (Sol/high), independent verifier `01a2057d-87bd-71a1-b2c0-2832cace0afd` (Sol/medium), and reviewer `01a2058a-0504-76b2-836b-c2c138ca5868` (Sol/high). The main agent owned HTTP, pack, CLI, file safety and final gates. No additional agents were started.

**PASS for the M4 synthetic-fixture gate.** Independent review and verification closed the identified blockers. The delivery branch is `rust-rewrite`.
