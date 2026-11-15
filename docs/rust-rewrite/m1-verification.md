# M1 independent verification

M1 fixture-only shelf verification **PASS** on the frozen release below. This is not full frontend/bootstrap or migration acceptance.

- Release SHA-256: 24186244a92c261ad496f8a1bf14d31ee00b843ba7a4baee53291cdddc0d7100, independently checked before and after final measurement.
- Final HTTP/fixture/benchmark artifact: C:\Users\MSI676~1\AppData\Local\Temp\reader-m1-obj45f0g\report.json.
- Rust direct-query raw samples/log: C:\Users\MSI676~1\AppData\Local\Temp\reader-m1-gates-9fc6948a5a924b07b199374d9e438aef\rust-scale.log.
- No generated databases, benchmark outputs or private data were committed. Only support Python and this report were edited by the verifier.

## Commands and outcomes

From rust/, all exited zero on final frozen source:

- cargo fmt --all -- --check
- cargo check --all-targets
- cargo clippy --all-targets --all-features -- -D warnings
- cargo test: **24 passed** — library 5, HTTP 2, shelf 14, scale 1, SQLite 2.
- cargo check --offline --locked --all-targets
- cargo test --offline --locked --lib --test shelf_contract --test sqlite_compat -- --nocapture: **21 passed**.
- cargo test --release --test shelf_scale -- --nocapture --test-threads=1: **1 passed**.

From repository root:

- python -m unittest discover -s rust/tests/support -p test_capture_reference.py -v: **6 passed**.
- python rust/tests/support/capture_reference.py --binary rust/target/release/reader_local.exe --benchmark: **exit 0**, no failures.
- git diff --exit-code -- Start.cmd '*.py' '*.mjs' web tests: unchanged legacy/frontend/reference files.

The Rust tooling emits the existing environment message “warn: could not canonicalize path C:\Users\MSI 676VN”; it did not affect exit codes or strict Clippy.

## Independent contracts and safety

The harness captured **3,344 cases**: 22 synthetic profiles ×152 requests. Complete structured JSON/status comparison covers all shelf sorts/directions, ties, filters, pagination/clamps, Unicode and malformed UTF-8, duplicate/blank parameters, Python integer syntax/diagnostics/precedence, numeric extremes, nested public HTML/cover output, malformed stored metadata and encoded identities. HTTP checks cover applicable exact security/content/cache headers, static bytes, cookie attributes, GET/HEAD lengths and empty HEAD bodies, supported/unsupported methods, Host/Origin, traversal and persistent GET→OPTIONS. Focused Windows junction escape tests pass.

All **1,112,064 Unicode scalar folds** match the independent Python 16.0.0 expectation, including 1,557 nonidentity folds. Native SQLite 3.51.1 passes exact feature SQL plus replay of current synthetic reference schema and actual group JSON/external-content FTS insert/update/delete triggers. Reference SQLite is 3.50.4.

Canonical fixtures are checkpointed, finalized in DELETE journal mode, closed and hashed before Rust opens them. Python App uses separate disposable mutable copies. All sealed fixture and 21 Python/frontend source hashes remain unchanged. Missing/unmarked/malformed-marker/unsupported-schema/same-name-wrong-index fixtures all fail without file creation or mutation. Runtime schema validation does not hash entire database bodies.

A separate WAL fixture, with immutable mode disabled, passed 100 snapshot reads while a synthetic writer committed 796 updates. Counts and returned rows remained consistent, and both committed totals 7/8 were observed. Writer latency p50/p95/max: **1.402/2.198/4.723 ms**.

A stalled POST advertised 16 MiB but sent one body byte. Another shelf GET succeeded with a 3 s socket timeout while that socket stayed open. Working-set increase was 20,480 bytes; private-commit increase 12,288 bytes; peak counters did not increase. This avoids a dangerous 1GiB allocation probe.

Rust authorizer tests forbid chapter-table reads and all mutations. The 50,000-book normal page returns 60 records, makes zero SQL casefold callbacks, and uses books_shelf_recent without a temporary sort. Python query tracing independently checks the same properties and excludes unrelated App maintenance threads.

## Matched measurements

Environment: Windows 11 build 26300 AMD64, 12 logical CPUs; rustc 1.100.0/cargo 1.100.0; CPython 3.14.7 MSVC 1944; Unicode 16.0.0; reference SQLite 3.50.4/native SQLite 3.51.1. Release optimization; no concurrent agent build/test/benchmark jobs during timing.

Dataset: **50,000 books, 3 cached chapter bodies, page 60**. Sealed DB 40,988,672 bytes/WAL 0; response 23,554 bytes; complete scale JSON agrees with Python. Each warm workload has 30 samples after a discarded warmup; p95 is nearest-rank. Raw samples are retained in the two artifacts above.

Warm p50/p95/max, milliseconds:

- Python query plus public JSON: **28.011/49.403/52.417**.
- Rust query plus public JSON: **8.931/10.198/11.432**.
- Python HTTP: **27.204/51.405/54.949**.
- Rust HTTP: **12.214/30.895/31.894**.

Rust/Python HTTP p50/p95 ratios: **0.449/0.601**, within the proposed 1.2× ceiling. Structural bounded-work gates also pass.

Three fresh-process samples, milliseconds; initialization is excluded from query/HTTP request timing:

- Python query+JSON: 40.941,12.637,13.198; median 13.198/max 40.941.
- Rust query+JSON: 8.7202,8.8191,9.2607; median 8.8191/max 9.2607.
- Python first HTTP: 29.5162,45.9553,30.0305; median 30.0305/max 45.9553.
- Rust first HTTP: 11.4496,14.0232,11.4194; median 11.4496/max 14.0232.

Measured Windows working set/peak: Python 57,786,368/97,304,576 bytes; Rust 7,528,448/10,559,488 bytes. Python private commit/peak 39,673,856/78,921,728 bytes; Rust 1,773,568/5,210,112 bytes. Python measurements include the verifier's allocations and full App with extra services; these are descriptive values, not a matched-service memory/startup speedup claim.

## RED evidence and limits

Before a Rust binary existed, the independent harness exited 1 solely for the missing implementation gate; report C:\Users\MSI676~1\AppData\Local\Temp\reader-m1-38qk322_\report.json. Expanded Python expectations were captured independently before fixes. Discovered Windows path, encoding, numeric/JSON diagnostic, HTML/XML/URL, body-drain and schema-validation defects were handed to the implementer and their regressions now pass.

Cold processes retain OS filesystem cache; three samples are not a cold p95. Timings describe this machine and corpus only. No production library, QT dictionary or live source network was used. No M2 lifecycle/writes/readers, complete frontend startup, STV/QT/document behavior or million-chapter performance is claimed. HTML/XML and malformed-JSON parity is established for the captured/focused corpus, not an exhaustive language proof.
