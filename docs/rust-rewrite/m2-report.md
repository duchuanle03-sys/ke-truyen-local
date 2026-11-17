# M2 lifecycle and offline reader

Recommendation: **PASS for the user-approved M2 scope**, including the explicit JSON-depth exception below. Final Rust gates, release HTTP/browser verification and both scale workloads passed. This is fixture-only M2, not full backend parity or production cutover. M3 is not authorized or started.

## Scope and safety

The fixture-only Rust backend adds explicit `--writable` for current unsealed synthetic libraries. The default M1 mode remains read-only. Existing Python, JavaScript/HTML/CSS, Node transport and launcher files remain reference inputs. No private library or browser profile is used. [Plan](m2-plan.md) records scope, ownership, acceptance tests and explicit offline/platform decisions.

## Files and behavior

Changed `.gitignore`, Rust Cargo manifests/lockfile, README and `src/{http,lib,library,main}.rs`. Added `src/{reader,lifecycle,windows_metadata}.rs`, `tests/m2_contract.rs`, `tests/support/m2_{capture,depth,rejection,selftest,browser,scale}.py` and the M2 plan/report/verification documents. Existing M1 tests and Python helpers remain unchanged. Pre-existing local `.codex` definitions are retained without staging them.

- Writable admission: validate synthetic marker/current schema and paths, bind loopback, hold the Python-compatible exclusive owner lock before SQLite access, register pinned Unicode folding, preserve WAL/foreign keys/schema and fail safely without migrations.
- Bootstrap and reader: genuine settings/groups/source catalogue/instance/book detail, full scoped TOC metadata, cached text/comic chapters and unchanged public-output sanitization. No startup or shelf/TOC chapter-body inventory.
- Durable scoped writes: progress, five-minute reader leases with sequence/expiry/capacity rules, notes CRUD retaining orphan notes, settings and book metadata including canonical asset references/pins/pending state. Python can reopen the resulting synthetic data.
- HTTP/assets: Host/Origin/method/body guards, bounded rejected-permission-body draining (at most 64 KiB/200 ms), existing JSON/status/header behavior, bodyless HEAD, cached covers/images with exact opened-handle validators, conditional requests and at most 64 KiB streamed chunks.
- Lifecycle: current cookie/token shutdown authentication, seeded durable pause/cancel intent, flushed acknowledgement, closed admission/listener, bounded connection cancellation and retryable drain/checkpoint while blocking jobs retain ownership. No scheduler or maintenance worker is implemented.
- JSON: ordinary object types preserved despite reserved serde keys; checked signed SQLite integers; Unicode numeric strings/Python coercion; UTF BOM/endian handling; integer digit limits and explicit empty-body semantics.

The user explicitly approved a maximum of 984 nested JSON containers, including the root. Excess input returns 400 before writes; Python instead can commit deeply nested settings and then fail HTTP reads. This deliberate exception is documented and tested in the [plan](m2-plan.md#user-approved-json-depth-exception). It is not blanket JSON parity.

## Delegation evidence

Existing M1 histories/configuration were evaluated before choosing concise fresh implementation and verification sessions; no M0/M1 session was reactivated. No architecture/parity discovery was repeated. At most three subagents are active, with separate source, test and review ownership. Runtime records, rather than task names, verify:

- `m2_implementation`: role `rust_implementer`, ID `01a1e6ee-5f57-7553-bfd3-ca44377a47b1`, `gpt-6.1-sol` / high.
- `m2_verification`: role `verifier`, ID `01a1e6ee-9e3e-7771-ba3e-99438b3eea9f`, `gpt-6.1-sol` / medium.
- Fresh `m2_review`: role `critical_reviewer`, ID `01a1e710-224f-74c0-8159-21beaf906b5c`, `gpt-6-astra` / high.

Session metadata gives the roles; the first recorded turn context gives actual model and effort. Local role definitions and previous completed histories are preserved. Same sessions handle scoped fixes; no duplicate reviewers.

## Final evidence

- Final release differential gate: 78 HTTP cases, nine encoding cases, 14 parser boundaries and 130 reader-capacity requests, zero failures; includes typed durable state, asset protection, original bytes, exact ETag, startup/owner exclusion, blocked writer, shutdown, restart and actual regenerable-cache lease/eviction controls (`reader-m1-b2yvru26/report.json`).
- Final release depth gate: supported stored/request/restart depths 127/128/256/512 match Python; 983 arrays plus root succeeds. Approved over-limit depths 984/985/1100, including a padded 2 MiB request, return 400 before mutation; subsequent reads/restart remain healthy (`reader-m1-8172u1fy/depth.json`).
- Final release browser smoke passed (`reader-m1-2x7v4ivc/browser.json`, `comic.png`): text reading/navigation/progress, bookmark save/delete, saved appearance and decoded cached comic work in an isolated temporary Chrome profile. Network diagnostics show completed target API responses; the unchanged missing favicon is the only excluded 404 console entry. The earlier timeout is recorded below.
- Final frozen measured-working-tree fingerprint: 26 Rust source/manifest/README/test/helper files, SHA-256 of compact sorted JSON mapping POSIX relative paths to file SHA-256: `526fd52110267fd5dcb0af25b68741c5224f73a6b13db12d7af51e1a8dfc3ad0`. Reconfirmed after both benchmarks. Final runtime release SHA-256: `2da7d3aa9580e2d214cf2055c60a2cd47d305711f53aec84db7e8e623286ce47`.
- Unchanged Python reference: 52 lifecycle/library/cache/image tests passed in 31.645 s; 26 HTTP tests passed in 20.852 s; 5 safe-output tests passed in 0.002 s. These validate reference behavior; they do not establish Rust parity by themselves.
- JavaScript syntax: `node --check` passed for `web/app.js`, `web/reader.js`, `web/captcha.js` and `stv_http_worker.mjs`.

See [independent verification](m2-verification.md) for full commands, raw artifacts, samples, memory and remaining limits.

Benchmark preparation is outside timing: the unchanged Python `Store.index_search(limit=8)` finishes the synthetic 512-body FTS backlog before identical finalized SQLite copies are made for either runtime. A bounded preparation helper asserts 500 books/100,000 chapter rows/512 downloaded bodies, unchanged cached-body fields/revisions, zero pending search rows and 512 indexed documents, then checkpoints and refreshes the synthetic marker hash. A fresh self-check passed (`reader-m1-bkrz7nhm/fixture-selfcheck.json`); repeating preparation on an idle fixture left its canonical hash unchanged. This isolated, reviewed benchmark-helper adjustment followed final runtime verification and changes no Rust source or correctness test. It prevents Python background indexing from confounding request timings.

## Measurements

Both completed runs use release SHA above, separate runtime processes, no overlapping build/browser/test/benchmark work and raw samples in OS-temp artifacts. Python runs the full reference App; Rust runs its M2 subset. These are equivalent HTTP workloads with different service scopes, not an overall application speedup or equivalent-service memory claim. Process-cold starts retain the OS filesystem cache.

M2 artifact: `reader-m1-mfma2lvy/scale.json`. Dataset: 500 books, 100,000 chapter metadata rows, 512 cached bodies with 28,341,760 payload bytes; one 50,000-chapter book, 498 books with 100 chapters and one with 200. One cached image is 65,604 bytes and one protected original is 18 bytes. FTS is idle (0 pending/512 documents). Final main database is 92,721,152 bytes for each runtime; no WAL file was present in the recorded size snapshot.

Times below are **p50 / p95 / maximum milliseconds, Rust versus Python**, 30 warm samples unless noted:

- Shelf: **3.347 / 30.333 / 32.527** versus **4.352 / 30.691 / 44.602**.
- Full 50,000-row TOC (5,866,158 response bytes): **199.230 / 223.850 / 246.245** versus **297.266 / 323.654 / 338.187**.
- Cached chapter: **9.102 / 31.120 / 31.941** versus **29.984 / 45.286 / 71.576**. First request was 44.206 versus 15.724 ms; it is reported separately from warm samples.
- Progress write: **31.858 / 62.751 / 65.633** versus **10.833 / 90.162 / 90.692**. **Rust's median is slower**; the cause was not established and no across-the-board improvement is claimed.
- Reader lease: **5.901 / 23.850 / 26.389** versus **9.199 / 36.968 / 48.198**.
- Concurrent SQLite writer: **1.305 / 2.571 / 17.744** (259 samples) versus **1.434 / 2.715 / 18.354** (160 samples); differing sample counts reflect the contention windows.
- Startup, only three process samples: **40.876 / 48.111 / 48.111** versus **268.103 / 290.511 / 290.511**. Descriptive only; Python initializes additional services.

M2 full-TOC process peak working set: Rust **73.914 MiB**, Python **91.094 MiB**. A separate 64 MiB streamed-file run used Rust **6.438 MiB** baseline and **7.844 MiB** OS peak working set, a **1.406 MiB** increase; sampled RSS and OS high-water values are retained. Its 1,754.291 ms elapsed time includes chunked/sampled transfer and is not a throughput claim. Stream disconnect and shutdown/drain checks passed. No startup/shelf/TOC body materialization was introduced; source inspection confirms scoped metadata-only queries.

Established M1 artifact: `reader-m1-b7n87h9y/m1-scale.json`, 50,000 books, 3 cached bodies, page size 60, 30 HTTP samples, 40,988,672-byte database and 0-byte WAL. HTTP p50/p95/max: Rust **10.539 / 35.313 / 35.682 ms**, Python **21.060 / 46.234 / 46.560 ms**; both return 23,554 bytes. Rust RSS/OS peak: **7.656 / 10.230 MiB**. Python verifier-plus-full-App RSS/peak: **38.957 / 72.375 MiB**, including capture/fixture allocations, so those memory scopes are not equivalent.

## Final check commands and results

On 2026-11-18, from `rust/`, all commands exited 0 on the held reviewed source:

```text
cargo fmt --all -- --check
cargo check --all-targets
cargo clippy --all-targets --all-features -- -D warnings
cargo test
cargo build --release
```

Final full `cargo test` after the transport fix: **44 passed**, zero failed: 22 library, 2 M1 HTTP, 3 M2 HTTP/depth/rejection, 14 shelf/output, 1 50,000-book scale, 2 SQLite compatibility tests. M1 HTTP includes 3,344 oracle cases. Test durations are not performance measurements. Final release build exited 0 on the same source (cached build 0.11 s); its SHA-256 is recorded above. Rustup printed an environment path-canonicalization warning for the home directory; commands and strict compiler lints succeeded.

`python -B -X utf8 -m unittest discover -s rust/tests/support -p m2_selftest.py -v`: initially **5 passed**, 2.695 s; after the isolated browser lifecycle helper change the verifier's focused suite passed **6 tests**, 3.117 s. Existing reference commands `python -B -X utf8 -m unittest discover -s tests -p test_server.py -v` and the corresponding `test_safe_output.py` command passed 26 and 5 tests. Combined unchanged reference selectors `test_shutdown.py`, `test_shutdown_restart.py`, `test_library.py`, `test_image_streaming.py`, `test_chapter_cache_api.py`, `test_chapter_access.py` passed 52 tests. Those reference sources and the four syntax-checked JavaScript files remain unchanged, so their baseline checks were not repeated after unrelated Rust changes.

Final release HTTP/state/process and depth artifacts: `reader-m1-b2yvru26/report.json` and `reader-m1-8172u1fy/depth.json`, zero failures. Instrumented serial browser flow passed for Python (`reader-m1-z_x2nhe0/browser.json`) and Rust (`reader-m1-2x7v4ivc/browser.json`), with all target TOC/chapter/progress responses completed successfully and clean Chrome exit. Instrumentation and lifecycle fixes preserve the original UI predicates, target and time budgets.

One prior browser run timed out while opening the TOC (`reader-m1-2fvnsii0`). It had no request-level diagnostics and was not reproduced in the final instrumented run. The independently fixed Chrome port-file/profile-cleanup races are not claimed to explain that timeout. This remains a recorded test-stability risk; no frontend or backend change was invented for it.

Environment reconfirmed: rustc/cargo 1.100.0, MSVC x86-64 Windows; Python 3.14.7, SQLite 3.50.4, Unicode 16.0.0; Node v26.13.0. Rust's bundled SQLite compatibility is checked against the exact schema/FTS/functions/triggers.

## Independent review

The Astra/high reviewer independently reproduced eight defects: cover-reference/pin loss, integer write corruption, coercion/error mismatches, missing image validation, JSON encoding rejection, reserved-key object corruption, integers unreadable by Python and BOM-only acceptance. Same-session fixes and independent regressions resolved the reproduced cases. Iterative parsing then replaced the original restrictive depth guard; the remaining behavior difference was explicitly approved by the user.

Final independent closure on release SHA-256 `2da7d3aa9580e2d214cf2055c60a2cd47d305711f53aec84db7e8e623286ce47`: **SPEC PASS against the approved M2 contract / QUALITY PASS**, no unresolved review blocker. It includes the final bounded-drain transport review, earlier supported-depth/no-write/restart/Python-reopening checks (`m2-depth-closure-gnmh0yam`) and exact typed parser/SQLite cases (`m2-typed-closure-y29ywbgm`). The approved exception remains explicit; this is not exhaustive JSON or production parity.

Review also inspected short scoped SQL transactions, lock/admission lifetime through disconnected blocking reads, streamed chunks, shutdown/checkpoint ordering and the isolated Windows API safety boundary. No additional security/data-loss blocker was reported in those reviewed paths. This does not cover excluded services or every malformed legacy value.

## Remaining limits

The opt-in synthetic backend is not a production launcher. Million-chapter, exhaustive malformed legacy metadata/JSON, live QT/STV/source networking, cache maintenance/eviction, download scheduling, imports/OCR and cutover are unverified or excluded. The approved depth exception, slower measured progress median and earlier unclassified browser readiness timeout remain explicit. No M3 work is included.

The first finalized-fixture M2 timing attempt (`reader-m1-b63f1b2m`) did not complete: a cold-start cleanup authentication probe observed Windows connection reset 10054 instead of a readable 403. Partial samples are excluded from final performance claims. A 48-request independent matrix (`reader-m1-my08kx4f/rejection.json`) confirmed Python 24/24 clean 403 responses, Rust 18/18 clean zero/buffered/immediate-split cases, and Rust 6/6 delayed two-byte failures before status, with the server remaining healthy. The fix matches Python's bounded permission-body drain. The independent expanded 54-case final release matrix passed with zero wire failures (`reader-m1-cfuu9abh/rejection.json`); Astra independently executed nine M1/M2 guard cases and retained SPEC/QUALITY PASS after inspecting limits, cancellation and owner lifetimes (`m2-drain-review-ei2ydbpr`). Timings are restarted only after the repeated final gates finish.

## Tracking and handoff

The allowlist still starts with `*`; only three specific M2 document exceptions were added. `git check-ignore --no-index -v` confirms source/manifests/test helpers/reports can be tracked while database, imported-book, dictionary, secret, log, target and generated-result paths remain ignored. Existing tracked private-data paths are absent according to `git ls-files`. Delivery uses an explicit 21-file source/test/document list and staged-diff inspection; pre-existing `.codex` files are excluded from this change.

Continue future work from this report, the approved plan/contracts and final verification evidence. Preserve the three agent histories and role definitions, but leave completed tasks inactive; do not reactivate M0/M1 discovery or carry full transcripts into future milestones. This M2 gate does not authorize M3 or production cutover.

All M2 implementation, verification and review tasks have completed; the final lifecycle check shows no active subagent work. Sessions were left inactive, not deleted. The isolated `rust-rewrite` branch and worktree are preserved. The delivery response records the final commit SHA and push result; `main` remains at `0dfff988880ac22d34d98c91c2e19ef226c829bb`.
