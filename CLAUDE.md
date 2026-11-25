# Kệ Truyện Local

Rust app in `rust/` serving the unchanged frontend in `web/`. Read [docs/architecture.md](docs/architecture.md) before changing storage, HTTP or startup code. Never modify, commit or test against the real `user-data/`; tests use temp copies of `rust/tests/fixtures/`.

# Maintenance

- Before committing, check for implementations superseded by the change and remove code, tests, fixtures, configuration and documentation that are no longer used.
- Confirm usage across application callers, documented tools, dynamic references and tests before deleting. Tests alone do not justify keeping an obsolete implementation.
- Preserve user libraries, imported files, dictionaries, backups and private browser profiles when cleaning source code.
- Commit directly to `main` (no feature branches or pull requests) and push to GitHub right after every commit unless the user explicitly requests otherwise.

# Large-library performance

- ĐÂY LÀ MỘT DỰ ÁN LỚN, MỘT THƯ VIỆN LỚN. Design for hundreds of books and hundreds of thousands to millions of chapter records, not a small demo library.
- Before every change, consider app startup, shelf/book/chapter loading, import/download/delete latency, memory, disk I/O and SQLite contention as part of correctness.
- Use indexed, scoped queries and bounded pagination/batches. Do not load or parse every chapter body on an interactive request or during startup. Heavy maintenance and migrations must run incrementally in the background and resume safely after interruption.
- Routine maintenance must scale with affected records/assets, not total library size. Moving a full-library or full-directory scan to a worker is insufficient; reserve those scans for explicit recovery or one-time upgrades, and verify normal operations avoid them.
- Keep SQLite write transactions and shared asset locks short. Parse HTML, fetch network data, enumerate directories and perform slow file deletion outside those locks; keep unavoidable atomic metadata operations small.
- For affected hot paths, add meaningful scale and concurrency regressions and measure representative workloads before claiming good performance. Report the dataset size, cached-body count, measured results and untested limits.
