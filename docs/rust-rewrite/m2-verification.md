# M2 independent verification

**PASS within the approved synthetic-fixture scope**, with the performance and test-stability qualifications below. Independent review closed SPEC/QUALITY PASS. No production library, dictionary, private profile or upstream source network was used. Verifier changed only new M2 tests/support and this report; no commits.

## Final provenance and gates

Release SHA-256: `2da7d3aa9580e2d214cf2055c60a2cd47d305711f53aec84db7e8e623286ce47`.

Final 26-file source/test fingerprint: `526fd52110267fd5dcb0af25b68741c5224f73a6b13db12d7af51e1a8dfc3ad0`. Algorithm: SHA-256 of compact, sorted-key JSON mapping relative POSIX paths to SHA-256 of file bytes. Scope: Rust source/tests/support Python plus Cargo.toml, Cargo.lock and rust/README.md. Reconfirmed after both benchmarks; runtime sources and release binary match their recorded hashes.

- Parent final `cargo fmt --all -- --check`, `cargo check --all-targets`, `cargo clippy --all-targets --all-features -- -D warnings`, full `cargo test` and release build exited zero. **44 Rust tests**: 22 library, 2 M1 HTTP, 3 M2 integration, 14 shelf, 1 scale, 2 SQLite.
- Unchanged Python reference baseline: **83 checks**; unchanged frontend: **4 JavaScript sources valid**. Harness self-checks: **6 passed**, including type-sensitive comparison, temp isolation, unreadable-response evidence, iterative deep fingerprints and debugger metadata readiness.
- Release main gate: **78 HTTP cases +9 encodings +14 parser boundaries +130 reader-capacity requests**, zero failures. `%TEMP%/reader-m1-b2yvru26/report.json`.
- Approved-depth gate: zero failures; stored/request/restart/Python readability at depths 127/128/256/512, accepted root+983 arrays, rejected overbound input before writes, healthy reads/restart and exact 2 MiB padded rejection. `%TEMP%/reader-m1-8172u1fy/depth.json`.
- Rejected-body transport: **54 strict wire cases**, zero failures, exact Python JSON/security headers, healthy process afterward. Includes cold/warm zero/buffered/split/50 ms delayed bodies, completed 64 KiB, oversized advertised body and stalled small body. `%TEMP%/reader-m1-cfuu9abh/rejection.json`.
- Unchanged frontend, serial Python then Rust: **PASS**, `%TEMP%/reader-m1-z_x2nhe0/browser.json` and `reader-m1-2x7v4ivc/browser.json`; screenshot `comic.png`. Shelf → Vietnamese text → next chapter/progress → bookmark save/delete → saved night appearance → decoded cached comic. Network/click evidence confirms the intended “Bắt đầu đọc” control and completed TOC/chapter/progress HTTP 200 responses. Browser plugin absent; authorized repository Chrome/Node CDP pattern, fresh profile, graceful exit/cleanup. Missing favicon 404 is recorded separately.

Canonical fixtures, managed images/originals, Python/frontend sources and Rust source/manifests remained unchanged where required. Mutable runtime copies retain expected committed state, valid SQLite integrity/foreign keys and backward Python readability. Tests cover scoped asset references/pins/pending/GC, original preservation, orphan notes, VIP progress refusal, integer no-write boundaries, JSON types/encodings, stale/durable reader leases, effective eviction control, process locks, busy SQLite retry, durable pause intent, response flush, stalled/disconnected I/O and shutdown drain.

## Approved limits and retained findings

The user approved **984 nested JSON containers including the root**, with overbound input rejected before writes as 400 `{"error":"Dữ liệu JSON lồng quá sâu."}`. This intentionally differs from Python 3.14's observed commit-then-500 failure at 984 arrays inside settings. Stored/request depth tests preserve this explicit contract; no claim of exhaustive JSON equivalence.

A real rejected-body race was found during the first timing attempt: Rust dropped an unread two-byte unauthorized body and reset the connection. Independent old-release RED: `reader-m1-0anr5rnd/rejection.json`, 6/6 delayed cases failed; Python cases returned 403. The shared bounded 200 ms/64 KiB permission drain now passes all 54 cases and independent review. **Partial timing artifact `reader-m1-b63f1b2m` is discarded from final measurements.**

An earlier concurrent-check browser run timed out waiting for reader readiness, showing “Đang mở mục lục…”: `reader-m1-2fvnsii0/browser.json`. It lacked network instrumentation and was not reproduced in final serial instrumented runs. No backend mismatch was evidenced. Separate proven Chrome startup/profile-cleanup races received state-based lifecycle fixes; UI assertions, targets and timeouts were unchanged. **The original intermittent reader timeout remains a test-stability risk and is not claimed fixed.**

Invalid/missing/sealed markers and bind failures require zero file effects. Valid unsealed writable startup may leave only lock/WAL/SHM coordination files after schema rejection; main DB/schema/managed bytes are preserved and ownership released. No immutable connection, manual WAL unlink or copied-DB workaround forces hash equality. Windows lock-file size is checked separately because its locked byte cannot be read by another process.

## Measurement method

Windows 11 build 26300, AMD64, 12 logical CPUs; CPython 3.14.7/reference SQLite 3.50.4, native SQLite 3.51.1; rustc/cargo 1.100.0. Release optimization. Both benchmark commands finished serially after all builds, correctness checks and browsers stopped; no competing agent benchmark.

M2 uses separate Python/Rust children and identical finalized synthetic SQLite copies. Before copying/timing, unchanged Python `Store.index_search(limit=8)` drains the fixture in at most 513 bounded batches; count/body hashes stay unchanged, FTS pending **512→0**, documents **0→512**, then checkpoint/close/marker hash refresh. Preparation is excluded. Parent inspected this isolated benchmark-helper revision; helper SHA `51f9fbb4b0668e7150a6f65b5d26ac3bf4008059b656d3eb76ba0586c61bd0c7`. Python still supplies the full reference App; Rust supplies the M2 subset. Startup/RSS describe those service scopes, not equal-feature memory/startup speedups.

Warm HTTP has one discarded warmup then **30 samples**, end-to-end receipt and JSON parsing; p95 is nearest-rank. Three fresh-process startup samples retain OS filesystem cache and do not establish a cold p95. Raw samples, counters, source/binary hashes and dataset sizes remain in the artifacts below.

## M2 scale results

Command: `python -B -X utf8 rust/tests/support/m2_scale.py --binary rust/target/release/reader_local.exe --samples 30`.

Raw artifact: `%TEMP%/reader-m1-mfma2lvy/scale.json`.

Dataset: **500 books /100,000 chapter metadata /512 cached bodies**, one 50,000-chapter book, 498×100 and one×200. Cached text+HTML **28,341,760 B (27.029 MiB)**. One cached image 65,604 B plus one original 18 B; FTS pending 0/documents 512. Main DB **92,721,152 B (88.426 MiB)**, WAL **0 B** for both measured copies. Full TOC response **5,866,158 B**, metadata only; shelf 29,130 B and cached chapter 55,525 B.

Warm **p50/p95/max milliseconds; Rust versus Python**:

- Shelf: **3.347/30.333/32.527** versus **4.352/30.691/44.602**.
- Full 50,000-row TOC: **199.230/223.850/246.245** versus **297.266/323.654/338.187**.
- Cached chapter: **9.102/31.120/31.941** versus **29.984/45.286/71.576**.
- Progress write: **31.858/62.751/65.633** versus **10.833/90.162/90.692**.
- Reader lease: **5.901/23.850/26.389** versus **9.199/36.968/48.198**.
- Concurrent SQLite writer: **1.305/2.571/17.744**, n=259, versus **1.434/2.715/18.354**, n=160. Same 30 foreground shelf/progress pairs and paced writer; differing duration produces differing commit counts, not a throughput comparison.

**Progress median is slower than Python in this run (31.858 versus 10.833 ms). No overall speedup is claimed.** First cached-chapter request after shelf/TOC work was Rust 44.206 ms versus Python 15.724 ms; these are not process-cold chapter timings.

Three startup-to-port samples, ms: Rust **40.876,39.042,48.111** (median/max 40.876/48.111); Python **290.511,264.808,268.103** (268.103/290.511), descriptive service scopes above.

Working set before/after workloads and OS peak: Rust **6.438/10.340/73.914 MiB**; Python **37.098/53.574/91.094 MiB**. Peak private commit: Rust **75.590 MiB**, Python **82.754 MiB**. Peak includes complete TOC materialization; it is scoped metadata, not chapter-body loading. Reviewed SQL omits body columns on startup/shelf/TOC; scale responses also assert their absence.

Separate streamed file: **67,108,864 B (64 MiB)**, exact byte/hash check, bounded client reads, disconnect and unread-stream shutdown checks passed. OS peak working set **7.844 MiB**, baseline **6.438 MiB**, increase **1.406 MiB**; peak private commit 1.750 MiB. The 108 memory observations supplement OS high-water counters. This intentionally paced integrity/memory probe took 1.754 s including local verification; it is not a raw throughput claim.

## Retained M1 workload

Invoked unchanged `capture_reference.benchmark(binary, fresh_output)` directly after M2 in a new process; no repeated full corpus just for timing. Raw artifact: `%TEMP%/reader-m1-b7n87h9y/m1-scale.json`.

Dataset **50,000 books /3 cached bodies /page 60**; DB **40,988,672 B (39.090 MiB)**, WAL 0, response 23,554 B. Warm n=30, p50/p95/max ms:

- Rust HTTP: **10.539/35.313/35.682**.
- Python HTTP: **21.060/46.234/46.560**.
- Python query + public JSON: **24.665/45.938/46.244**.

Rust/Python HTTP p50/p95 ratios **0.500/0.764**, within the proposed 1.2× ceiling. Fresh Rust direct-query timing was not added; its structural/indexed regression remains in the full suite.

Three fresh-process first-HTTP samples, ms: Rust **28.739,12.453,20.700**, Python **31.700,41.999,33.432**. Startup descriptive samples: Rust **12.516,11.651,13.094**, Python **377.107,399.570,398.267**. Working set/OS peak: Rust **7.656/10.230 MiB**; Python **38.957/72.375 MiB**. This established helper's Python memory scope includes the verifier plus full App/fixture allocations; no matched-service RSS claim.

## Remaining limits

One machine, small sample counts, cached filesystem, synthetic metadata/body distributions and one 50,000-row TOC do not establish million-chapter or worst-case latency guarantees. Progress median and first cached read regressions above remain measured limitations. Original intermittent browser readiness risk is retained. Live library migration, source/STV/QT/import behavior and production launchers are outside M2 and were not exercised.
