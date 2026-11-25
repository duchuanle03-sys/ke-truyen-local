# M5 public sources, STV and durable downloads

## Delivered scope

The Rust backend serves the existing source and download routes from writable synthetic libraries. QQ, Truyện Full and Hako retain canonical source URLs, complete TOCs and sanitized chapter output. Source refresh prepares chapter metadata outside the write transaction and rejects changes that would move or drop existing chapter identities. The scheduler retains two slots with at most one STV background job, persists progress at chapter boundaries, and keeps CAPTCHA answers, cookies and verification ownership in memory.

The native client is opt-in through `--native-sources` on an unsealed synthetic fixture. A scripted fixture can supply exact URL responses without network access. The existing Python backend, Node transport, JavaScript frontend and production data remain outside the Rust fixture gate.

## Review and fixes

- The Windows TLS spike first failed on Schannel credentials. Rustls passed a generated-CA loopback test for TLS identity, literal signed paths, cookie header order, fresh IPv4 verification, response bounds, cancellation and observed disconnect. The native transport uses a separate bounded executor for DNS so a full HTTP blocking pool cannot deadlock lookups. The regression saturated a one-thread HTTP pool during a successful local HTTPS exchange.
- Ordinary image misses now reserve from the byte budget before publishing. Managed temporary files and crash recovery use the existing asset candidate table. The zero-budget case still serves an image without caching it. The fresh response uses a strong byte-derived ETag, while cache hits retain the existing file validators. Offline images are pinned before their job checkpoint advances.
- A started worker retains durable cancellation intent on trash and prevents purge until settlement. The callback checks the owning job and non-trashed book before storing content or pins. Source refresh validates existing chapter identities and prepares changed rows outside the write lock, then compares affected metadata before committing.
- STV throttling checks cancellation before a paced request starts. The scheduler acknowledges successful CAPTCHA only after the verified requeue commits, including automatic SQL retry. A private per-challenge shared result survives delayed job registration without keeping an unbounded history or replaying answers. Malformed input is rejected before allocating an HTML tree beyond the reference's depth and element bounds.

## Verification coverage

The eight unchanged Python suites for sources, STV API/HTTP/transport/scheduling and download lifecycle passed 105 tests. Synthetic HTTP differential evidence covers source validation, recorded QQ/Full/Hako (including a 1,470-chapter Full TOC), STV bootstrap and source translation, ordinary image budgets and conditional responses, durable job pagination and state changes, offline bodies and image pins, and a same-job CAPTCHA resume. A forced process kill after 14 saved bodies preserved a durable checkpoint of 10 in both engines. Recovery loaded only the remaining 16 chapters of the same range and preserved offline ownership, reading progress and notes.

## Remaining gates

Fixture validation establishes protocol behavior for the tested cases. The Windows browser launch path, real upstream headers and cookies, live STV CAPTCHA/TLS behavior and production-library workloads still need controlled acceptance before retiring Node. The native client uses fresh HTTP connections and its standalone public text decoding covers UTF-8 and Latin-1. Those transport differences remain part of live compatibility verification.

## Final Rust gates

From `rust/`, `cargo fmt --all -- --check`, `cargo check --locked --offline --all-targets`, and `cargo clippy --locked --offline --all-targets --all-features -- -D warnings` passed on frozen source. `cargo test --locked --offline --no-fail-fast` passed 90 tests with three opt-in drivers ignored in the standard run. The larger M5 source error type initially exhausted the M2 depth-test stack. Boxing its optional context restored the original stack and JSON limits, and the full run passed the original depth and restart checks. `node --check` passed for the three tracked frontend files and the reference Node transport.

The independent verifier built the locked offline release and ran `cargo test --locked --offline --release --lib --test source_contract --test download_recovery --test download_workers`. All 62 tests passed. Two helper drivers were ignored in the standard release run. The release executable SHA-256 is `4791a27c707052015687a852f8a1e83795401bd8a9d2d82872bfd66465163ce2`. The frozen 44-path source and fixture fingerprint is `bc34e16090ebb6edcd19b283f7064994f5afe5c647a54509228dc03faf0cc6d3`. The benchmark driver SHA-256 is `e3bf9d92176410946600a50e7262ddc981edf00fa54d84987263d36bbaa603f3`.

Rust Anything 1.5.1 analyzed the Python source and recorded nine synthetic HTTP cases from the Python backend. Python replay and optimized Rust replay each passed 9/9 with zero skips and no fallback or normalization. Evidence is in the temporary directory `reader-m5-ra-b41809c7e7b34b9688b62985373164e5` under `rust/app-cases/replay-report.md`.

## Mixed scheduler benchmark

The paired run used identical 42,086,400-byte synthetic databases with 10,004 books, 100,000 chapters and 512 cached 4,096-byte bodies. The queue contained 10,000 pending STV jobs behind a held STV lane and 50,000 historical jobs. One Full job ran 256 synthetic 4 ms chapter callbacks while the test timed 30 overlapping cached reads. Windows 11 build 26300 had 12 logical CPUs. The Python reference used Python 3.14.7 with SQLite 3.50.4.

- Submission to first callback took 105.511 ms in Python and 34.443 ms in Rust. Submission return took 71.851 ms and 34.437 ms, respectively. Each admission timing is a single observation.
- Cached reads took 10.693/33.331/34.340 ms at Python p50/p95/max and 7.654/20.262/20.939 ms in Rust.
- Current and peak working sets were 38,744,064 and 43,012,096 bytes in Python, and 10,100,736 and 12,550,144 bytes in Rust. Peak private memory was 29,704,192 and 7,028,736 bytes, respectively.
- Both implementations ended with 50,001 completed and 10,001 paused jobs. Read output hashes matched. Cached bodies were unchanged and SQLite and FTS integrity checks passed.

The run measured scheduler and cached-reader entry points. It excluded HTTP framing, source parsing, body writes, image I/O and background maintenance. Admission timing is not a throughput measurement. The raw report is `reader-m1-hc7uvbcp/scheduler.json` in the OS temporary directory, SHA-256 `5cd54c19b6464a055a36155c6c5e7ca2d458eee5590c67882c08d8699376b6b0`. The canonical database SHA-256 is `b17df69f62959c793abe34020c37f5ed5ddba29f388ac97f01930ca6714db687`.

## Ownership

The main session owned sources, HTTP and shared library integration. The existing STV implementer (`01a2057d-21cf-7022-9b62-c9c5302d012e`), verifier (`01a2057d-87bd-71a1-b2c0-2832cace0afd`) and critical reviewer (`01a2058a-0504-76b2-836b-c2c138ca5868`) were reused. The separate `m5_downloads` implementer owned scheduler code and its focused worker tests. Independent review closed the HTML, DNS, cache reservation, purge and CAPTCHA findings, including an injected commit failure for a late member of a shared CAPTCHA cohort.

**PASS for the M5 synthetic-fixture gate.** The delivery branch is `rust-rewrite`. Controlled live acceptance remains open for Node retirement.
