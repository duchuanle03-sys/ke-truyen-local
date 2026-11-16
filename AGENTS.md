# Maintenance

- Before committing, check for implementations superseded by the change and remove code, tests, fixtures, configuration and documentation that are no longer used.
- Confirm usage across application callers, documented tools, dynamic references and tests before deleting. Tests alone do not justify keeping an obsolete implementation.
- Preserve user libraries, imported files, dictionaries, backups and private browser profiles when cleaning source code.
- After every local commit, immediately push to the configured GitHub remote and branch unless the user explicitly requests otherwise.

# Subagent workflow

- Start each milestone from saved architecture decisions, behavior contracts and the previous handoff. Delegate only unanswered questions; do not automatically repeat architecture/parity discovery or whole-repository scans.
- Use no subagents for routine work that the orchestrator can complete directly. For substantial milestones, the default team is one implementer, an independent verifier and an independent reviewer, with at most three concurrent subagents and one writer per file set. Explicit user instructions take precedence.
- Before spawning, check existing agents and reports. Resume an agent for related follow-ups and fix rounds; start fresh when its accumulated history would outweigh a concise task brief. Do not keep every agent's context across every milestone.
- Give new agents isolated context (`fork_turns: "none"`) and a bounded brief: task, interfaces, owned files, relevant references, acceptance tests and report path. Do not inherit the entire parent conversation or ask every agent to reread the repository.
- Keep implementation, verification and review independent. Preserve the reference contracts; do not change expected behavior to make tests pass. Reuse the milestone's implementer and reviewer through scoped fix rounds rather than spawning duplicate reviewers.
- Run focused checks during development and review fixes. Freeze source before final full verification and benchmarking; keep timing runs separate from builds and other benchmarks. Repeat checks only when changes, failures or unresolved concerns justify them, and ensure final evidence matches the reviewed source.
- Keep a concise milestone handoff with agent IDs/status, ownership, interfaces, decisions, tests, measurements, open findings and commit/push status. Carry this evidence forward instead of full transcripts.
- At the milestone gate, end obsolete tasks and leave completed agents inactive. Preserve reports, useful history and reusable role definitions. Use available lifecycle controls; verify status before claiming an agent was closed or removed, and do not repeatedly poll an inconclusive status.
- Completing a milestone does not authorize the next one. Wait for the user's authorization before starting further implementation.

# Large-library performance

- ĐÂY LÀ MỘT DỰ ÁN LỚN, MỘT THƯ VIỆN LỚN. Design for hundreds of books and hundreds of thousands to millions of chapter records, not a small demo library.
- Before every change, consider app startup, shelf/book/chapter loading, import/download/delete latency, memory, disk I/O and SQLite contention as part of correctness.
- Use indexed, scoped queries and bounded pagination/batches. Do not load or parse every chapter body on an interactive request or during startup. Heavy maintenance and migrations must run incrementally in the background and resume safely after interruption.
- Routine maintenance must scale with affected records/assets, not total library size. Moving a full-library or full-directory scan to a worker is insufficient; reserve those scans for explicit recovery or one-time upgrades, and verify normal operations avoid them.
- Keep SQLite write transactions and shared asset locks short. Parse HTML, fetch network data, enumerate directories and perform slow file deletion outside those locks; keep unavoidable atomic metadata operations small.
- For affected hot paths, add meaningful scale and concurrency regressions and measure representative workloads before claiming good performance. Report the dataset size, cached-body count, measured results and untested limits.
