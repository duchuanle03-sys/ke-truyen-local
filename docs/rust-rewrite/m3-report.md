# M3 durable maintenance

**PASS for the approved synthetic-fixture M3 scope.** The Rust backend now serves scoped search and HTTP cache maintenance, edits and original restore, trash and purge, and confirmed SQLite compaction. Bounded background work maintains FTS, cache and asset state, including staged-file recovery and the approved startup trash behavior. [Plan and approved scope](m3-plan.md).

Base: `fda4d90a04e3cf0b1e3a535093533754e5c6140e` on the linked `rust-rewrite` worktree. Private data, the Python reference and the JavaScript/HTML/CSS frontend are outside the write set.

## Contract evidence

- The unchanged Python reference passed 128 tests from `test_{search_content,trash,library,chapter_cache,chapter_cache_api,chapter_cache_scale,asset_cleanup,image_cache,image_streaming,download_recovery,downloads}.py` (68.344 s). Existing 100k-chapter/512-body and 100k-image-metadata reference scale checks passed in that run. These historical samples are separate from the final matched M3 release measurements.
- Rust Anything 1.5.1 analyzer ran against Python. Search capture and Python replay passed 5/5; the M2 control failed 5/5 for missing endpoint behavior; the first M3 search build passed 5/5. No normalization or noise exceptions. Raw temporary evidence `reader-m3-ra-c77e45dcc1d948a7a3cabce923fedf4a`.
- Final Rust Anything release replay passed **16/16** exactly: search 5, edit/trash 5, cache/compaction 6. Python passed the same cases with no unstable outputs. Temporary edit and cache evidence `reader-m3-ra-d5b0eb39f3764cbbb56397bf38fec074` and `reader-m3-ra-30b8209a7e03409cab515e335d4374bf`.
- Four pinned legacy migrations passed the focused Rust schema tests, including progress retention, restart, database integrity, unknown-catalog refusal and whole-transaction rollback/retry. Canonical reference profiles were generated exclusively in test directories.
- Independent search verification passed, including FTS cursor restart and injected failure rollback. Independent lifecycle checks passed for edits, original restore, trash, archive, purge rollback and pause/cancel scope. Independent cache and recovery checks passed for protected/offline/legacy content, eight-body eviction, 32-image batches, Windows deletion retry, concurrent file lease, reservation recovery and a crash after unlink.

## Frozen verification

Final release binary SHA-256: `a869c1ebd361ee4d77f013faf447579b63a33eadb2248d1f17ff82595859cb07`. Final Rust source, test, manifest, and README fingerprint: `38f197b59e0294c00b3277dcb15fce0b37955731a02dce05a3f495b1bb146bc2` (41 files, SHA-256 of compact sorted JSON mapping POSIX relative paths to raw file hashes).

From the repository root, `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`, `cargo check --offline --manifest-path rust/Cargo.toml --all-targets`, and `cargo clippy --offline --manifest-path rust/Cargo.toml --all-targets --all-features -- -D warnings` passed. `cargo test --locked --offline --manifest-path rust/Cargo.toml` passed all **60 tests**, including earlier M1 and M2 suites. `cargo test --locked --offline --release --manifest-path rust/Cargo.toml --test cache_contract --test maintenance_contract --test recovery_scale --test search_contract` passed all **four release differential contracts**. Four JavaScript syntax checks passed, and a scoped Git diff confirmed no changes to the reference sources, original Python tests or frontend. The fixture data and build products remain ignored.

## Review and focused fixes

- Reject out-of-range chapter path numbers before SQLite binding instead of saturating to another chapter. Preserve Python's edit HTML fallback for hidden custom tags and chapter response field order.
- Reconcile durable cancelled/trashed active jobs before accepting requests. Expiry rechecks the chosen book under the write lock, and the user approved brief startup visibility until the worker deletes eligible expired books.
- Same-scheme relative image URLs can hold the only offline copy. Independent pre-fix test observed lost references, pin and PNG; the corrected debug build preserved all three on normal reindex and cleanup (`reader-m1-zgzkzoit`).
- An accepted shutdown used to miss the listener notification if the client reset the connection before SQLite committed. A two-event handoff now signals after durable acceptance and connection closure in either order. Independent pre-fix RED (`reader-m1-b1tlwtuz`) and corrected debug GREEN (`reader-m1-_b7nxyu0`) preserve paused job state and prove process/lock release.
- M2's direct Python eviction regression now gives Python sole control of that particular fixture quota during its measurements. The original assertions are retained. M3 independently tests the live worker's eviction.
- Final review identified a clear-query work amplification with an old active lease and fresh cache rows. The native regression with 1 versus 10,001 cached bodies measured 168 versus 60,168 SQLite VM steps before the fix and 137 versus 138 afterward. The completion probe used 125 steps. Both statements use an explicit LRU time range.
- A real Windows directory junction in a synthetic asset root was deferred without moving or deleting the target; a separate valid candidate completed in the same check (`reader-m1-p910xkc8`). Concurrent external directory replacement during metadata checks and file operations remains unverified.

Independent reviewer verdict: **SPEC PASS / QUALITY PASS for the approved fixture scope**, with no outstanding blocker. Retained limits include the atomic per-book cascade, zero-size entries ahead of eligible image budget candidates, and untested concurrent external directory replacement.

## Scale gate

The first timing attempt stopped at a harness import error before collecting samples (`reader-m1-pb7i2ids`). The corrected harness completed the Python half of the first profile, then stopped on a native HTTP 500 after a chapter-cache limit write committed under contention (`reader-m1-xn8ky29t`). Cached reads and progress writes also failed. An isolated diagnostic build captured SQLite WAL locking error `SQLITE_PROTOCOL` (15), with sidecars disappearing between operations (`reader-m1-ggwa86pg`, `reader-m1-s217cygu`). Neither attempt is accepted as a matched benchmark.

The writable library now retains one idle, non-transactional WAL connection and closes it before dropping the process lock. Read-only mode is unchanged. A deterministic regression reproduced WAL turnover and proved lifetime/checkpoint/lock cleanup. An independent mixed-process regression passed 24 eviction/rearm cycles, 135 cached reads and 122 progress writes on debug (`reader-m1-twqologf`), and the focused optimized replay passed (`reader-m1-ea8g4eet`). The same reviewer approved startup, compaction and teardown after this change. All 35 library tests and strict Clippy passed after the fix. Exact Rust Anything replay repeated 16/16, and the optimized M2 and M3 integration selection passed all seven tests on the final release. The unchanged M1 and standalone shelf/compatibility integrations were last run in the full 60-test gate before the writable-only WAL fix.

Measure with `python -B -X utf8 rust/tests/support/m3_scale.py --binary rust/target/release/reader_local.exe --samples 30`. Final raw evidence is in the OS-temporary directory `reader-m1-w6txyt7z`. Each paired run uses two fresh copies of the same prepared fixture, no overlapping builds or timing runs, and validates SQLite/FTS integrity and canonical/source/binary/helper hashes. Rust serves its fixture subset; Python runs the reference App with upstream fetch disabled. Startup uses three process launches with the system file cache intact. Maintenance latency includes scheduling and 25 ms state observation. Process I/O reports OS counters, which can include network I/O. The high-fanout purge is one sample per runtime and profile and verifies request overlap and intact unrelated state.

All three final pairs completed. Environment: Windows 11 build 26300, 12 logical CPUs, Python 3.14.7 and SQLite 3.50.4 reference, Rust 1.100.0 MSVC and bundled SQLite 3.51.1. Every profile has 500 books, a 50,000-chapter book, 100,000 image metadata records and zero physical image files. The 512-body profiles hold 2,097,152 bytes of cached text; the 10,000-body profile holds 40,960,000 bytes. Timings below are **p50 / p95 / maximum ms**, with Rust first and Python second. Warm requests and foreground maintenance each use 30 samples. Contended request counts vary with the elapsed workload.

### 100,000 chapters, 512 bodies

Initial SQLite 38,932,480 bytes. Contended samples (read/write) Rust 268/233, Python 238/224.

- Startup median/max **20.754 / 42.864** versus **263.279 / 284.295**.
- Shelf **2.856 / 18.500 / 20.328** versus **4.156 / 28.922 / 32.150**.
- Full TOC **198.772 / 213.131 / 222.019** versus **286.620 / 315.812 / 321.117**.
- Cached chapter **17.903 / 29.213 / 30.464** versus **11.578 / 47.374 / 51.464**.
- Search **5.021 / 26.875 / 27.916** versus **5.599 / 30.028 / 32.169**.
- Eight-body eviction **224.737 / 369.025 / 371.966** versus **233.305 / 455.707 / 536.442**.
- 32-image cleanup **221.065 / 299.445 / 309.346** versus **261.510 / 473.620 / 669.547**.
- Routine purge **26.156 / 59.495 / 71.974** versus **53.899 / 81.582 / 92.368**.
- Contended chapter **42.284 / 94.718 / 139.309** versus **69.577 / 131.356 / 394.045**.
- Contended progress **52.328 / 109.671 / 150.601** versus **80.724 / 128.402 / 365.389**.

One 50,000-chapter purge with 128 cached bodies took **225.081 / 250.513 ms**. Peak RSS was **78,430,208 / 94,564,352 bytes**. Process read/write totals were Rust **490,306,944 / 113,119,936 bytes**, Python **1,166,163,625 / 162,096,872 bytes**. Live WAL at the end of measurement was **107,672,112 / 0 bytes**.

### 1,000,000 chapters, 512 bodies

Initial SQLite 132,558,848 bytes. Contended samples (read/write) Rust 265/224, Python 213/209.

- Startup median/max **14.577 / 32.423** versus **279.990 / 319.467**.
- Shelf **4.037 / 30.864 / 32.139** versus **4.753 / 29.452 / 38.453**.
- Full TOC **199.573 / 221.446 / 221.910** versus **295.135 / 325.056 / 360.296**.
- Cached chapter **23.589 / 34.309 / 37.228** versus **18.722 / 60.379 / 90.076**.
- Search **16.618 / 32.329 / 34.223** versus **11.754 / 32.795 / 46.087**.
- Eight-body eviction **203.718 / 406.451 / 419.377** versus **207.828 / 581.594 / 618.557**.
- 32-image cleanup **236.799 / 288.868 / 301.936** versus **266.768 / 412.414 / 424.497**.
- Routine purge **36.412 / 55.185 / 58.928** versus **57.571 / 80.253 / 88.927**.
- Contended chapter **46.761 / 95.176 / 127.920** versus **81.218 / 142.848 / 412.795**.
- Contended progress **59.754 / 113.879 / 161.121** versus **81.809 / 127.870 / 269.918**.

One 50,000-chapter purge with 128 cached bodies took **219.695 / 271.510 ms**. Peak RSS was **78,315,520 / 94,539,776 bytes**. Process read/write totals were Rust **508,215,885 / 126,388,812 bytes**, Python **1,131,480,288 / 161,725,268 bytes**. Live WAL at the end of measurement was **115,574,272 / 5,524,952 bytes**.

### 1,000,000 chapters, 10,000 bodies

Initial SQLite 263,692,288 bytes. Contended samples (read/write) Rust 231/212, Python 221/221.

- Startup median/max **14.444 / 46.788** versus **293.211 / 340.690**.
- Shelf **3.565 / 28.541 / 29.100** versus **10.345 / 29.510 / 32.304**.
- Full TOC **213.885 / 230.739 / 231.247** versus **288.345 / 304.412 / 322.879**.
- Cached chapter **26.803 / 33.619 / 37.373** versus **26.786 / 74.733 / 83.955**.
- Search **29.941 / 35.539 / 36.456** versus **29.267 / 40.028 / 46.154**.
- Eight-body eviction **182.137 / 876.375 / 890.879** versus **256.122 / 516.214 / 580.253**.
- 32-image cleanup **246.220 / 868.825 / 1,727.390** versus **256.755 / 738.982 / 835.680**.
- Routine purge **41.332 / 71.980 / 84.375** versus **60.207 / 93.476 / 115.155**.
- Contended chapter **48.723 / 147.690 / 1,862.229** versus **82.464 / 236.697 / 526.073**.
- Contended progress **59.368 / 218.570 / 1,423.011** versus **82.748 / 173.956 / 473.577**.

One 50,000-chapter purge with 512 cached bodies took **602.587 / 645.620 ms**. Maximum overlapping progress request **813.082 / 685.759 ms**. Peak RSS **78,348,288 / 94,437,376 bytes**. Process read/write totals Rust **1,969,870,607 / 584,711,844 bytes**, Python **3,659,566,055 / 1,410,740,340 bytes**. Live WAL at the end of measurement **566,846,112 / 0 bytes**. Post-shutdown WAL size was not sampled in the scale runs. Rust's worse eviction, cleanup and writer p95s and large live WAL are follow-up performance concerns. The results do not establish an overall latency improvement.

The verifier independently recalculated the medians, nearest-rank p95s and maxima. Raw artifact file SHA-256:

- `100k-512.json` `97fa00c6e60d466b9566a97b378dacfbe295ff8e4e71ad695f5c1e89be9f5098`
- `1m-512.json` `54adde50ab2352801869e04e728df48fec0af01838da7c9f0d527ec990d27b03`
- `1m-10k.json` `6a111e33e79ab94e64452696b13477301b04c6366c25af3f1c14b470c6849bce`

The exact batch assertions, SQLite VM regression, indexed SQL inspection and metadata-only TOC projection give bounded-work evidence. End-to-end internal statement/decoder counts and physical disk I/O were not instrumented. File-import API latency, live-source fetch races, private library migration and arbitrary unsupported legacy catalogs remain outside this gate.

## Agents

Resumed M2 implementation and verification briefly to check suitability. Their recorded models remained GPT-6.1 Sol, so these completed sessions were left inactive. New bounded sessions on the updated configuration:

- Implementer `01a2057d-21cf-7022-9b62-c9c5302d012e`, `rust_implementer`, GPT-6.2 Sol/high.
- Verifier `01a2057d-87bd-71a1-b2c0-2832cace0afd`, `verifier`, GPT-6.2 Sol/medium.
- Independent reviewer `01a2058a-0504-76b2-836b-c2c138ca5868`, `critical_reviewer`, GPT-6.2 Sol/high, verified from initial runtime metadata.

The verifier became inactive when the implementation or a review blocked measurement. Only these three M3 roles were used for implementation, verification and review. Subsequent fixes and checks reused them. No architecture or parity session was created. At delivery all three are inactive.

Delivery includes `.gitignore`, the M3 plan and report, Rust Cargo/README, new `assets`, `chapter_cache`, `chapters`, `schema` and `search` modules, integration changes in `http`, `lib`, `library`, `lifecycle` and `reader`, four Rust integration entry points and six M3 Python support files, and the small M2 fixture coordination adjustment. Git tracks only the explicit source/test/doc list. The branch remains `rust-rewrite`; the delivery response records the commit and push result. M4 requires separate authorization.
