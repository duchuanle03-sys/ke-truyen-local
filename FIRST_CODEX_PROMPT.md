# Initial task to paste into Codex

We are beginning a FULL RUST BACKEND REWRITE of ke-truyen-local. Keep the existing JavaScript/HTML/CSS frontend. The current Python/Node implementation remains an immutable reference during migration.

First, read the existing repository AGENTS.md and RUST_REWRITE_POLICY.md. Inspect the local environment using `rustc --version`, `cargo --version`, `rustup show active-toolchain`, and confirm the Windows MSVC linker with a disposable Cargo hello-world build. Check the current working tree and branch, and do not touch real user-data/.

Use subagents with the following roles:
1. architect (read-only): map Rust crate architecture, module ownership, API boundaries, migration order.
2. parity_auditor (read-only): enumerate current HTTP/SQLite/QT/STV/cache/lifecycle behavior contracts and existing regression tests.
After architect and parity_auditor return their findings, delegate the draft architecture and contract checklist to critical_reviewer (read-only) to identify data-loss/security/concurrency risks. Wait for the review and synthesize a single milestone plan. The orchestrator should produce a short architecture ADR, a route-to-module mapping, a feature-parity checklist, baseline measurement plan, and the first concrete implementation task with file ownership and a test gate.

Do NOT start a sweeping rewrite, do NOT modify the existing Python/JS production files, and do NOT create/modify a user database during this initial task. If prerequisites are missing, report exact missing commands/components and safe next steps. Use a concise handoff with links to project files rather than dumping huge transcripts. Only after approval of the milestone plan should rust_implementer and verifier begin narrowly scoped work.
