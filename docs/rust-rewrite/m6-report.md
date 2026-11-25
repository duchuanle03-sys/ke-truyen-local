# M6 documents, backup import and Windows integration

## Delivered scope

The Rust backend now serves `/api/import/file`, `/api/import/backup`, `/api/books/{id}/shortcut`, `/api/books/{id}/file` and the complete `/api/capabilities` document/OCR/sources payload. These modules port `documents.py`, `legacy_formats.py`, `pdfocr.py`, `shortcuts.py` and the import parts of `server.py`/`library.py`:

- `documents.rs`: TXT/HTML/XHTML/Markdown, EPUB, DOCX/ODT/FODT, FB2/FBZ, ZIP/CBZ.
- `legacy_formats.rs`: RTF, HWPX, HWPML.
- `imports.rs`: upload, backup and the `_put_book` port.
- `platform.rs`: PDF/OCR and Desktop shortcuts through the unchanged `windows/*.ps1`.

To match the reference, the importer reproduces a subset of Python's `html.parser` and an ElementTree-style XML tree. Codec fallback uses exact single-byte tables generated from Python 3.14 `encodings`, plus `encoding_rs` for GB18030/GBK/Big5/Shift_JIS/EUC.

User decisions recorded in [m6-plan.md](m6-plan.md):
- Output matches the reference exactly for well-formed files only.
- Backup import commits in batches of about 2,000 rows instead of one transaction.
- Shortcuts keep the Python launcher target until M7.
- One implementer plus one independent reviewer.

## Behavior notes

- Unsupported upload names are rejected before anything is written. A failed import queues its upload for the existing asset collector, and a stalled body is disconnected after the 30 s idle timeout.
- Handlers that take longer than 30 s no longer lose their connection. Before this fix, the idle read timer dropped any request whose handler ran past 30 s, such as large imports or OCR, which the reference allows up to 600 s. A 56 s backup import now returns 200.
- Backup books are parsed outside the write lock and their asset references are published by the background indexer. A failing book aborts the import, earlier batches stay committed, and rerunning completes it idempotently.
- Two pre-existing quirks are kept for parity:
  - Text before the first heading becomes a chapter titled with the upload's SHA-256 stem.
  - A blank OCR page is stored without a body, so reading it returns 400.

## Review and fixes

The independent reviewer reported one blocker, two important findings and three minor ones. All were fixed and the fix round passed:

- **Deep XML (blocker):** a 50,000-level document overflowed the stack and aborted the server. Depth is now limited while the tree is built.
- **Quadratic tokenizer:** finding the end of raw-text elements no longer copies the rest of the document.
- **Body-read timeout:** the idle-timeout exemption now starts only after the body has been read.
- **Minor:** the PDF stream now leases the key the collector checks; a missing PDF returns Python's 404; null/false sources and other falsy backup values follow Python's `or`.
- **Own finding:** a declaration-slicing panic on multibyte text was fixed and covered by a regression test.

Accepted and unchanged:
- The identical-content concurrent-upload race, which exists in the reference too.
- Finding `pythonw` through PATH. This goes away in M7.
- fancy-regex backtracking limits on very large attributes, which the reviewer did not verify.

## Verification

All checks below ran on frozen source, from `rust/`:

- `cargo fmt --all -- --check` passed.
- `cargo clippy --locked --offline --all-targets --all-features -D warnings` passed.
- `cargo test --locked --offline --no-fail-fast` passed: 67 unit tests and every integration contract from M1–M6, with the four opt-in drivers ignored.
- The unchanged Python suites `test_documents`, `test_legacy_formats`, `test_pdfocr` and `test_shortcuts` passed 48 tests.

The `tests/support/m6_documents.py` differential produced identical results from the Python and Rust HTTP servers in all five cases:

| Case | Events | What it covers |
|---|---|---|
| documents | 100 | 18 well-formed formats including encodings and images; 14 rejections: bomb, traversal, broken ZIP, binary, DTD, 150-level HTML, 50,000-level FB2, duplicate spine, fake PDF, unsupported names; truncated and oversize uploads; reimport keeping progress; trash/restore |
| backup | 16 | folder and ZIP backups, groups and notes, rejected paths, reordered TOC |
| pdf | 6 | real `Windows.Media.Ocr` (en-US) on a generated two-page PDF, the `/file` stream |
| crash | 2 | process killed during a 41 MB import, then restart: SQLite intact, orphan collected or book committed |
| stall | 1 | partial upload disconnected after about 30 s |

A Windows COM test created the shortcut in a private temporary desktop, not the user's. The `.lnk` matched Python's except for embedded access timestamps, and the `.ico` matched byte for byte. The fallback and rerun behavior also matched.

## Large-library benchmark

`tests/support/m6_scale.py` ran on Windows 11 build 26300 with 12 logical CPUs, Python 3.14.7 and the release binary (SHA-256 `053fc5cd79141c451ff78a9482bee2fe5543c6e7e170cd310bc467d567a8f978`). The library had 10,000 books, 100,502 chapter rows and 512 cached 4 KB bodies. A reader opened a cached chapter every 20 ms throughout each import. These are two paired runs on the final source:

| | Python | Rust |
|---|---|---|
| 40.9 MB TXT, 6,000 chapters | 10.6–11.0 s; reader p95 0.7–1.1 s, max 6.8–7.3 s | 1.51–1.52 s; reader p95 52–57 ms, max 0.9–1.0 s |
| Peak working set during TXT import | 405 MB | 241 MB |
| Backup, 200 books / 100,000 chapters | 29.8–31.7 s; 2–3 reader requests **timed out at 10 s** | 20.0–20.7 s; reader p95 0.54–0.80 s, max 1.25 s, no errors |
| Startup working set | 39 MB | 10 MB |

An earlier run of the same workload on a less loaded machine was faster for both engines: backup Python 12.8 s vs Rust 7.7 s, TXT 6.1 s vs 1.6 s. Treat absolute times as machine-dependent; the ratios and reader tails held in every run.

Not measured:
- EPUB/DOCX corpora and image-heavy CBZ imports at scale.
- OCR throughput on long scans; only the 2-page probe ran.
- Million-chapter backups.
- Live PDFs with installed languages other than en-US.

## Remaining for M7

- Switch `platform::launcher` to the Rust executable and replace `Start.cmd`.
- Run the full route and frontend browser flows, and the cutover/rollback rehearsal.
- Decide which reference quirks, including the two above, remain part of the contract.

**PASS for the M6 synthetic and Windows-integration gate.**
