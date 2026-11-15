# Milestone 1 results

Status: implementation, fresh verification and measurements pass for M1; Git finalization is recorded in the execution handoff. Independent source review has cleared all eight findings.

Scope: fixture-only Rust HTTP shelf slice. JavaScript/HTML/CSS, Python/Node production sources, `Start.cmd`, private databases, libraries, originals, dictionaries, backups and profiles remain unchanged. M2 is not authorized or started.

## Files and ownership

- `.gitignore`: replace the broad Rust allowlist with source/manifests/tests and specifically reviewed Markdown. Keep databases, generated fixture/measurement data, build output, logs, secrets and private directories excluded.
- `rust/Cargo.toml`, `Cargo.lock`, `README.md`: one package, pinned dependency graph and fixture-only instructions.
- `rust/src/{main,lib,library,http,casefold,entities}.rs`: CLI/runtime, read-only shelf SQL, HTTP/public output, pinned Unicode16 properties and public HTML5 entity data.
- `rust/tests/{common,http_contract,shelf_contract,shelf_scale,sqlite_compat}.rs`: focused contracts, native SQL probes, Windows containment and release scale checks.
- `rust/tests/support/{capture_reference,test_capture_reference}.py`: independent synthetic fixture, differential HTTP, safety and measurement harness.
- Reviewed migration documents: ADR, behavior checklist, M0 evidence, milestone plan, this report and independent verification report. The existing rewrite policy/prompt are versioned without changing their contents.

The implementer owns Rust source/focused tests; the verifier owns independent Python support/evidence; the orchestrator owns tracking, plan and final report. Existing local agent configuration files are preserved outside the M1 commit.

## Completed first-slice behavior

- Explicit fixture/web-root/port arguments, loopback-only binding, synthetic marker and pinned Unicode/index-definition validation. Missing or unsupported fixtures fail without creation or writable fallback. No default user-data path, startup database hashing or schema maintenance.
- Read-only/query-only SQLite plus authorizer denial of writes and chapter-table access. Counts and page use one snapshot. Indexed/scoped shelf queries materialize at most200 books, preserving all specified filters, stable sorts/ties, default60/max200 limits, offset normalization and all six envelope fields.
- Full Unicode16 scalar casefold mapping, Python-compatible URL/query/int parsing including4300-digit limit, metadata/groups/Boolean handling and recursive safe public output.
- GET/HEAD shelf and unchanged static files; bodyless HEAD with matching representation length, Host/Origin guards, localized errors, security headers, cookies, OPTIONS403 and captured unsupported-method behavior. Python float JSON notation and parsing precision are covered.
- Hyper/Tokio caps connections and blocking jobs separately at64, retains job admission until completion, uses30-second idle timeouts and rejects bodies without draining or advertised-length allocation.
- The selected bundled SQLite is3.51.1 versus Python3.50.4. Native tests exercise actual persisted schema objects, group JSON triggers and external-content FTS insert/update/delete, plus generated-column/expression-index/trigram probes.

## Verification

Final Rust commands run from `rust/`:

```powershell
cargo fmt --all -- --check
cargo check --all-targets
cargo clippy --all-targets --all-features -- -D warnings
cargo test
cargo check --offline --locked --all-targets
```

All commands exited0. `cargo test` passed **24 tests** (5 library, 2 HTTP, 14 shelf, 1 scale, 2 native SQLite), including the independent **3,344 HTTP cases across22 profiles**. Offline focused tests passed21; harness self-tests passed6. Every one of the1,112,064 Unicode scalar values was compared with the pinned Python reference. Windows junction containment, authorizer restrictions, exact native SQL and no per-row SQL folding passed.

Reference commands already passed: `python -B -X utf8 -m unittest discover -s tests -p test_bookshelf.py -v` (7), `test_server.py` (26), `test_safe_output.py` (5): **38 tests**. `node --check` passed for app.js, reader.js, captcha.js and stv_http_worker.mjs.

Independent final commands/results, dataset-qualified measurements and detailed safety evidence are recorded in [m1-verification.md](m1-verification.md). The final release SHA-256 is `24186244a92c261ad496f8a1bf14d31ee00b843ba7a4baee53291cdddc0d7100`.

## Final measurements

Same synthetic workload: **50,000 books, 3 cached chapter bodies, page60**,30 warm samples per measurement. SQLite file40,988,672 bytes; WAL0 for the sealed benchmark; UTF-8 public JSON23,554 bytes. Normal pages used `books_shelf_recent` without a temporary sort, converted one page, performed zero SQL folding callbacks and did not access chapters.

- HTTP Python reference p50/p95/max: **27.204 /51.405 /54.949 ms**. Release Rust: **12.214 /30.895 /31.894 ms**. Observed warm ratios0.449/0.601 are within the proposed20% no-regression ceiling; these30 samples do not establish a general speedup guarantee.
- Query plus public JSON, Python p50/p95/max: **28.011 /49.403 /52.417 ms**. Rust release test: **8.931 /10.198 /11.432 ms**. Both time query/serialization after initialization; the native test and Python harness are separate measurements on the same corpus.
- Three fresh-process first HTTP samples, Python:29.516/45.955/30.030 ms; Rust:11.450/14.023/11.419 ms. Three first-query/public-JSON samples, Python:40.941/12.637/13.198 ms; Rust:8.720/8.819/9.261 ms. OS filesystem caches were retained; three samples are not a meaningful cold p95.
- Rust HTTP process observed RSS **7,528,448 bytes (7.18 MiB)**, peak10,559,488 (10.07 MiB); private commit1,773,568, peak5,210,112 bytes. Reference RSS57,786,368, peak97,304,576 bytes includes the verifier process, full Python App and fixture/capture allocations. These are different process scopes, so no memory or whole-app startup improvement is claimed.
- Descriptive startup samples: Rust14.738/13.862/12.699 ms; full reference App486.625/448.720/451.698 ms. The services differ; these samples are not a matched startup comparison.
- WAL regression observed both valid totals7/8 while a separate synthetic writer committed796 times: writer p50/p95/max1.402/2.198/4.723 ms. Counts and page remained consistent. This is a small concurrency regression, not a million-chapter mixed-load benchmark.
- Rejected-body regression advertised16 MiB and sent1 byte. A separate GET completed while that socket stayed open; observed RSS/private changes20 KiB/12 KiB stayed below the8 MiB test ceiling. All five invalid-fixture modes failed closed, and sealed DB/source/frontend hashes remained unchanged.

Raw samples and environment metadata are retained in the generated temporary report referenced by the verification document; generated data are excluded from Git.

## Independent review and resolutions

- R1 stalled rejected body: remove the tiny_http adapter/dependency; Hyper discards unread bodies without its blocking drain/allocation. Add parallel-GET and memory regression while an incomplete socket stays open.
- R2 URL safety/preservation: match Python suffix/IP/port/bracket rules and preserve signed paths/query encoding, including supported IPvFuture behavior.
- R3 HTML parsing/entities: quote-aware tokens, script/style raw-text handling and complete public HTML5 names/numeric rules; do not decode discarded script/style bodies. Visible over-limit numeric references retain Python's error.
- R4 metadata shapes: preserve failure for scalar/null/key-containing list/string raw metadata, and success for accepted empty/unrelated containers.
- R5 JSON diagnostics: compare delimiter/trailing-comma/Unicode/multiline/escape/literal positions; rewind EOF-truncated literals as well as malformed complete tokens.
- R6 SVG: namespace-aware permitted elements, surrounding whitespace and strict XML entities; keep cover raster-only behavior.
- R7 fixture support: validate pinned Unicode and canonical index definitions, preserving quoted SQL literals; reject same-name incompatible indexes and reparse escapes using bounded metadata work.
- R8 SQLite gate: replay real schema and exercise its actual group/FTS triggers rather than relying on a toy feature probe.

All eight findings are addressed in the final scoped source review, with fresh verifier evidence on the same frozen binary. Focused regressions recorded genuine RED/GREEN failures. Superseded tiny_http, WHATWG URL and incomplete HTML entity decoder dependencies/code were removed. The Python/Node reference remains required until later full cutover and is not obsolete at M1.

## Decisions and limits

Two orchestration decisions: the implementer owns focused Rust tests while the verifier independently owns the Python oracle; replace the unsafe HTTP adapter with Hyper/Tokio. The latter costs additional dependencies/build time and preserves the required bounded serving behavior.

This establishes only the tested M1 surface. Bootstrap/settings/sources/groups/cover/reader routes, writes/migrations, QT, STV, downloads, maintenance, import/OCR, shutdown and production launcher/cutover remain unimplemented in Rust. No complete frontend startup or million-chapter performance claim is made. Exotic malformed HTML/XML/JSON, nonfinite/surrogate metadata and general protocol fuzzing are not exhaustively characterized. Pinned Unicode/SQLite and future write/rollback compatibility remain gates for later milestones.

REA was considered; source inspection and synthetic runtime probes sufficed. No shipped-artifact investigation was necessary. No private database or library data is part of the commit.
