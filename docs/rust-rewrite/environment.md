# Rust rewrite preparation evidence

Date: 2026-11-16, Asia/Saigon. This is an observed preparation baseline, not Rust parity or production performance evidence.

## Repository and safety

- Workspace: `C:/Users/MSI 676VN/Downloads/truyện/reader-local-rust`.
- Branch: `rust-rewrite`; HEAD `0dfff988880ac22d34d98c91c2e19ef226c829bb`.
- `git worktree list --porcelain` confirms a separate linked worktree; the main checkout is `../reader-local`, branch `main`, at the same starting commit.
- Initial `git status --short` was empty. Origin is `https://github.com/duchuanle03-sys/ke-truyen-local.git`. No fetch, commit or push was needed.
- Read `AGENTS.md`, `RUST_REWRITE_POLICY.md`, `FIRST_CODEX_PROMPT.md`; delegated read-only architecture and contract reports in parallel, then an independent critique after both returned.
- Created only preparation Markdown in the repository and a disposable Cargo probe under the OS temporary directory. Existing production sources and frontend are unchanged. No private database, imported content, dictionary, backup or browser profile was inspected or opened.
- Existing tests use disposable synthetic data. No production entrypoint or external source request was used as a baseline.

## Installed environment

- `rustc --version`: `rustc 1.100.0 (0ef1bc386 2026-11-09)`.
- `cargo --version`: `cargo 1.100.0 (3acc79c0c 2026-11-05)`.
- `rustup show active-toolchain`: `stable-x86_64-pc-windows-msvc (default)`.
- Installed rustup components: cargo, clippy, rust-docs, rust-std, rustc, rustfmt for x86_64-pc-windows-msvc.
- Python `3.14.7`; `sqlite3.sqlite_version` `3.50.4`; `unicodedata.unidata_version` `16.0.0`.
- Node.js `v26.13.0`.
- Platform reported by Python: `Windows-11-10.0.26300-SP0`; Intel64 family 6/model 186/stepping 2; 12 logical processors. Physical RAM and full CPU model were not collected; optional CIM queries were access-denied.

## Native Windows build probe: passed

Disposable location: `%TEMP%/reader-rust-msvc-probe-14b5e6b28d954f1e89052ba97c98f65b` (not repository source).

Successful sequence:

```powershell
cargo init --bin --edition 2024 --vcs none --name reader_rust_msvc_probe $probeRoot
cargo build --manifest-path (Join-Path $probeRoot 'Cargo.toml') --target x86_64-pc-windows-msvc --offline -vv
& (Join-Path $probeRoot 'target/x86_64-pc-windows-msvc/debug/reader_rust_msvc_probe.exe')
```

Build exit 0; Cargo reported 1.74 seconds; the resulting native executable printed `Hello, world!` and exited 0. This proves the MSVC linker/Windows native standard-library build works even though `cl` and `link` were not on the interactive shell PATH. It does not validate compiling bundled SQLite, OpenSSL or other native dependencies.

Nonblocking sandbox warnings: Rust could not canonicalize the home path; Cargo could not save last-use metadata to its global cache; Git could not read the global ignore file. No escalation or installation was necessary for the successful probe. Third-party Cargo downloads/cache availability and future native dependency builds remain unverified.

## Python SQLite capability probe: passed, limited scope

An in-memory database successfully executed `CREATE VIRTUAL TABLE probe_fts USING fts5(value, tokenize='trigram')`. Compile options include ENABLE_FTS3/FTS4/FTS5 and THREADSAFE=1. This probe did not establish the selected Rust SQLite build's behavior. M1 must additionally execute exact `trigram case_sensitive 1`, JSON, generated-column, expression-index and trigger SQL against that Rust build.

## Executed focused checks

```powershell
python -B -X utf8 -m unittest discover -s tests -p test_bookshelf.py -v
python -B -X utf8 -m unittest discover -s tests -p test_chapter_cache_scale.py -v
python -B -X utf8 -m unittest discover -s tests -p test_server.py -v
node --check web/app.js
node --check web/reader.js
node --check web/captcha.js
node --check stv_http_worker.mjs
```

- Shelf: **7 tests passed**, 13.794 seconds, including stable pages, Unicode/literal query behavior, snapshot/writer coexistence and indexed large shelf behavior.
- Chapter-cache scale: **1 test passed**, 22.432 seconds, including bounded LRU, FTS reclaim, explicit compaction and SQLite quick_check.
- HTTP: **26 tests passed**, 22.410 seconds, including shelf pagination, Host/Origin guards, traversal, OPTIONS connection reuse, import preservation and safe image/cache paths.
- All four JavaScript syntax checks exited 0.
- Total: **34 Python tests passed**. These are existing reference tests, not Rust differential tests.

## Measured synthetic workloads

Shelf ([test_bookshelf.py](../../tests/test_bookshelf.py)): **50,000 books, 3 cached chapter bodies, page size 60**. Five timed default pages: median **22.546 ms**, serialized page **24,154 bytes**, zero per-row normalization calls and no chapter-table reads. Additional single observations: name sort **13.584 ms**; substring **85.142 ms**; group filter **32.734 ms**; last page **24.389 ms**. One-time shelf-index upgrade **994.778 ms** on the synthetic fixture. The small sample count does not support a useful p95.

Chapter cache ([test_chapter_cache_scale.py](../../tests/test_chapter_cache_scale.py)): **100,000 chapter records in 2 books, 512 cached bodies**, batch size **8**. Candidate selection **4.373 ms**; eviction **36.847 ms**. Removed **172,200 body bytes** and **86,104 normalized bytes**; FTS merge released **426,130 payload bytes**. Physical SQLite bytes: before and after eviction **47,030,272**; after merge **47,099,904**; after explicit compaction **46,514,176**. FTS payload and physical DB bytes are distinct metrics; eviction alone did not shrink the SQLite file.

Workloads ran sequentially in debug/reference Python, during source analysis, with OS cache state uncontrolled. Measurements are observations from one run, not accepted regression budgets or a comparison with Rust. No RSS, WAL-size, I/O, HTTP latency distribution or full-app startup baseline was captured here.

## Outstanding gates

Not run: complete test suite, browser/DOM flows, real source/STV/TLS/CAPTCHA checks, Windows OCR/shortcut integration, QT million-entry performance, million-chapter mixed concurrency/crash tests, Rust backend fmt/clippy/tests (there is no backend crate yet), packaging or rollback rehearsal.

Before M1 acceptance, collect matched-scope Python and release-Rust measurements with at least 30 warm samples and three fresh-process runs, explicit body counts and cache conditions. Report warm p50/p95/max and cold samples/median/max; three samples do not establish a meaningful cold p95. See the [plan](../superpowers/plans/2026-11-16-rust-backend-migration.md) for later scale gates.

At M0 preparation time the repository used a deny-all `.gitignore` and these analysis documents and the existing policy/prompt were local ignored files. No allowlist, staging or commit was performed during M0. M1 subsequently authorized a narrow source/document allowlist; see [M1 results](m1-report.md). Private-data and generated-output directories remain excluded.
