# Rust backend rewrite policy (append or reference from existing AGENTS.md)

- Goal: fully replace the Python backend and the Node.js STV transport with Rust; preserve the existing JavaScript/HTML/CSS frontend and HTTP API. Python remains a temporary reference implementation, not a permanent production dependency.
- Do not edit or remove legacy production files before full Rust feature parity is demonstrated; do not mutate or commit user-data/, qt.sqlite3, library.sqlite3, imported books, offline content, backups, private profiles, secrets, or actual downloaded files.
- Run Rust implementation in a separate Git worktree/branch. Rust components live in rust/; preserve existing Python implementation for differential reference until a defined cutover.
- Orchestrator owns task decomposition and interface approval; delegate read-only discovery and review in parallel, but only one writer per file set. Use at most three concurrent subagents.
- Every milestone has an explicit behavior contract, input/output interface, tests, baseline performance and acceptance gate. Never equate successful compilation with parity.
- Contract-critical domains: QT tier/scope precedence and snapshot/COW; SQLite schema/WAL/FTS5/trigram and durable download state; cache protected vs regenerable data; request priority, STV CAPTCHA/session and >=10-second source throttling; local loopback auth; controlled shutdown; reader progress; import/OCR.
- Do not rewrite frontend in Rust/WASM or change UX unless expressly approved. Keep JS API compatibility.
- Verify rustc/cargo plus a native MSVC hello-world build before starting. Avoid replacing the working Start.cmd until the Rust launcher is proven.
- Before committing, run focused tests, Rust fmt/clippy and relevant differential tests; independent reviewer should report blockers. Keep commits narrow. Push behavior follows existing repository AGENTS.md.
- Use saved architecture contracts, test fixtures, milestone status and concise handoffs to reduce token usage; never have all agents repeatedly reread the entire repo.
