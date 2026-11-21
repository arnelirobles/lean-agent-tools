# Brain as its own project

Date: 2026-11-21. Status: approved by Arnel, Nov 21 2026.

## Why

Brain is a local list of what went wrong and is not fixed yet, shared by every session on the machine: test failures, blockers, questions for a person, rulings, notes. Today it ships inside lean-agent-tools, so only lean-agent users get it, and it carries the test queue that only lean-agent's `test-phase` uses.

Arnel wants brain for anyone: it installs and works without lean-agent, and team sync gets added later.

This is the second split. The first (Nov 7 2026) moved everything that is not Markdown out of lean-agent into lean-agent-tools. This one moves brain out of lean-agent-tools.

## Goals

- Brain installs and works alone: the `brain` command, the MCP server, the brain page, the Lean Brain VS Code extension, and brain's own Claude Code hooks (session id on items, digest at start and after a compaction, answers handed back to the session that asked).
- lean-agent-tools works without brain. With brain installed, test failures, blockers and hook notices land in brain as they do today.
- The only contract between the two is the `brain` command line. Each repo versions and releases on its own.
- No data is lost. Items, sessions, created records and notices stay in `brain.db`. Queue and run history move to lean-agent-tools' own database.

## Non-goals

- Team sync. This change only keeps the door open (see Team sync later).
- PyPI, Homebrew or npm packaging.
- Changing what an item is, its kinds, or how the page looks beyond removing the queue and run views.
- PR #126 in arnelirobles/lean-agent (the brain assistant). It gets rebuilt on the brain repo later.

## Anti-goals

These would make it a failure even if it runs:

- Brain knowing about tests. After the split, the brain repo has no `jobs` or `runs` table code, no test project, lane, filter or dotnet logic. The one exception is the one-version forward of `brain queue` (see Compatibility).
- lean-agent-tools importing brain's Python or opening `brain.db`. The one exception is the one-time copy of the queue history (see Moving the data).
- A switch that leaves Arnel's machine running two brain MCP servers, or losing queued jobs mid-batch.

## Approaches

- **A. Brain keeps the queue, runs and flaky tracking; lean-agent-tools requires brain.** Smallest move and no command changes. Rejected: anyone installing brain for Codex or Cursor gets test commands they can never use, team sync has to carve tables out, and lean-agent-tools stops working without brain.
- **B. Brain becomes a plain item store; the queue, runs and flaky tracking move to lean-agent-tools with test-phase.** Brain is generic and fully syncable, and each plugin works alone. Costs a command rename (`brain queue add` becomes `test-phase queue add`) and a one-time data move. **Picked by Arnel.**
- **C. Brain takes test-phase too.** Rejected: brain would carry a 4,500-line dotnet, vitest and gradle runner nobody outside lean-agent needs.

## What moves where

| Piece (today in lean-agent-tools) | After |
|---|---|
| `lib/brain/store.py`, `items.py`, `records.py`, `activity.py` | brain repo |
| `lib/brain/report.py` | brain repo, minus the job and run parts of `digest`, `resume` and `stats` |
| `lib/brain/cli.py` | brain repo, minus `queue`, `run-add`, `flaky-clear` |
| `lib/brain/web/*` (the brain page) | brain repo, minus the queue and run sections |
| `tools/brain`, `tools/brain-mcp` | brain repo, `brain-mcp` minus the `brain_queue_*` tools |
| `vscode/` (Lean Brain extension, reads `brain.db` directly) | brain repo, minus `queueTree.ts`, `lanes.ts` and the run views. Supersedes lean-agent #143 |
| `hooks/brain-resume.py`, `hooks/serve-start.py`, the brain half of `hooks/session-env.py` (session start and end, answers on prompt) | brain repo, as brain's own hooks |
| `docs/specs/2026-10-20-team-brain-design.md`, `docs/specs/2026-10-24-brain-web-page-design.md`, `docs/plans/2026-10-24-brain-web-page.md` | brain repo |
| `plugin.json` `mcpServers.brain`, `userConfig.serve_mode`, `userConfig.worktree_roots` | brain's `plugin.json` |
| `lib/brain/queue.py`, `lib/brain/flaky.py`, the `jobs` and `runs` tables | lean-agent-tools, new package beside test-phase (name is the builder's) |
| `test-phase`, `holdout.sh`, `lean-stats`, every other hook and tool | stay in lean-agent-tools |
| `session-env.py` PATH line and the `lean-stats collect` on SessionEnd | stay in lean-agent-tools |

## The contract between the two

lean-agent-tools reaches brain only by running it.

- **Finding brain** (my call): `command -v brain` first, then the newest `~/.claude/plugins/cache/*/brain/*/tools/brain`. `hookkit.tool_path("brain")` changes to this lookup. Not found: skip quietly, and test-phase prints each failure in its log instead of filing it.
- **Calls lean-agent-tools makes after the split**, the whole contract:
  - `brain add` with the body on stdin, `brain list --json` (with `--open --kind --project --tag --cycle`), `brain close ID --resolution --note`. These are the item calls `tools/test-phase` makes today.
  - `brain notice add` and `brain created add`, new, replacing the direct imports in `lib/hookkit.py` and `hooks/created-log.py`.
  - `brain session infer --json`, new (see The queue in lean-agent-tools).
  - The queue calls test-phase makes to brain today (`queue requeue|next|finish|cancel|list|route`, `run-add`, `flaky-clear`) become calls into its own queue package. They leave the contract.
- **Notices and created records** (my call): run with a 1 second timeout, as today. On a timeout, a missing brain or an error, the record is dropped and the hook goes on. The hookkit self-test "locked brain returns within 2 s" stays.
- **Parsed output** (my call): add `brain list --json` and have test-phase read that, instead of matching `^#\d+ open` lines across two repos. Each item carries at least `id`, `kind`, `status`, `project`, `repo`, `branch`, `cycle`, `run_id`, `ref`, `title`, `tags`, `first_seen`, `last_seen`.
- **Contract test**: the brain repo keeps a list of every call lean-agent-tools makes and runs each against a scratch database in CI. A change that breaks one fails brain's CI, not Arnel's next batch.

## The queue in lean-agent-tools

- Own database (my call): `~/.local/share/lean/tests.db`, overridable with `LEAN_TESTS_DB`, holding `jobs` and `runs` with today's columns and ids.
- Commands (my call): `test-phase queue add|list|wait|cancel|bump|move|next|route|requeue|finish`, `test-phase run-add`, `test-phase flaky-clear`, `test-phase stats`. Plain `test-phase` still drains. This keeps Arnel's rule of one drainer under one name.
- MCP (my call): lean-agent-tools ships a small MCP server named `tests` with the queue tools (`queue_add`, `queue_list`, `queue_bump`, `queue_cancel`, `queue_route`, `queue_next`). The JSON-RPC loop is copied from `brain-mcp`, not shared.
- Session on a job (my call): `jobs` keeps `session_id` and `session_cwd`. The queue takes them from `$LEAN_SESSION_ID` and `$LEAN_SESSION_CWD`. When those are unset and brain is found, it calls `brain session infer --json`, which prints the one open session started in the current folder (today's `infer_session` in `lib/brain/items.py`), or empty when there is none or more than one. Without brain, the job has no session.
- Digest after a compaction (my call): `brain digest` today lists queued and running jobs. lean-agent-tools prints those lines in its own SessionStart note (startup and compact), as a separate block with its own cap, not inside brain's digest budget.
- Items keep their `cycle` column, so `brain list --cycle C` still lists a cycle's failures. `items.run_id` stays as a plain number with no reference to a table brain no longer has (SQLite never enforced it here).
- Flaky tracking and `test-phase stats` (my call) keep their flaky columns. They read runs from their own database and `test-fail` and `flaky` items from `brain list --json`, grouped by `run_id`. Without brain those columns read "brain not installed".
- The drainer lock stays at `~/.local/share/lean/test-phase/drainer.lock` (`tools/test-phase:2151`), so a 1.x drainer still running blocks a 2.0.0 one until it exits.

## Brain's hooks without hookkit

`lib/hookkit.py` is lean-agent-tools' shared hook library (1,972 lines with its self-tests). Brain's three hooks need only: read the event JSON, add context, append to `$CLAUDE_ENV_FILE`, and call brain's own library directly (today they run `brain session start|end` and `brain answers` as subprocesses). Brain gets its own small hook helper and a hand-written `hooks.json` (my call), not a copy of hookkit or the HOOK-literal build.

- Events, matchers and timeouts are a straight port of today's entries in `hooks/hooks.json` and `hooks/dispatch.json` (my call). `serve_mode` stays `manual` by default.
- Brain's SessionStart hook writes `LEAN_SESSION_ID`, `LEAN_SESSION_CWD` and brain's tools folder on PATH to `$CLAUDE_ENV_FILE`. lean-agent-tools' `session-env.py` keeps its own `LEAN_SESSION_*` and PATH lines, since its queue needs the session without brain.
- Each plugin appends only its own lines, under its own copy of the file lock in `session-env.py:120` (my call). Which plugin runs first does not matter: the session lines carry the same value and each PATH line adds a different folder. The order test at `session-env.py:253` splits into one test per side that checks only that side's lines.
- Switches keep their names: `LEAN_BRAIN_DB`, `LEAN_BRAIN_RESUME`, `LEAN_SESSION_ENV`.

## Other tools (Codex, Cursor)

They get the CLI and the MCP server, no hooks. The README shows the MCP config pointing at a clone's `tools/brain-mcp`. Answers handed back to the asking session stay Claude Code only in this version.

## Compatibility

- `brain queue ...`, `brain run-add` and `brain flaky-clear` in brain 1.0 forward to `test-phase` when it is found, with one stderr line saying the command moved. Without test-phase they exit 2 with that line. Removed in brain 1.1.
- MCP tool names change. `mcp__plugin_lean-agent-tools_brain__brain_add` becomes `mcp__plugin_brain_brain__brain_add`, and the queue tools become `mcp__plugin_lean-agent-tools_tests__queue_*`. Sessions read tool names at start, so only written text needs updating.
- Text that names `brain queue add` and must change: `skills/tools/SKILL.md`, the messages in `hooks/script-note.py` and `hooks/wait-loop.py` and their self-tests, Arnel's `~/.claude/CLAUDE.md` ("Tests and open items"), and the memory notes that quote it. Arnel approves the CLAUDE.md wording.
- `brain.db` stays at `~/.local/share/lean/brain.db`.
- Versions: lean-agent-tools goes to 2.0.0 (the queue command and MCP names break). Brain starts at 1.0.0.

## Moving the data

- On first open of `tests.db`, when it is empty and `brain.db` has `jobs` or `runs`, lean-agent-tools copies both tables, ids kept. This is the one read of `brain.db` it ever makes, through a read-only SQLite open (`mode=ro`). `brain export` cannot do it: it covers only `items` and `tags` (`lib/brain/report.py:225-250`). The copy goes away in lean-agent-tools 2.1.
- The copy refuses while `brain.db` has queued or running jobs (my call). It prints how many and says to drain them with lean-agent-tools 1.x first, and every queue command exits 2 until then. Nothing is copied halfway.
- Brain never drops `jobs` or `runs` from an existing database. New databases do not get them.
- The switch happens with an empty queue and no drainer running.

## Building the brain repo

- Made from lean-agent-tools with history kept for the moved paths only (`git filter-repo --path ...`, my call), so blame and log survive. The last split cloned the whole history; this one keeps only brain's files.
- License MPL-2.0 (Decided by Arnel). The extension's `package.json` says MIT and points at the old lean-agent repo; both get corrected.
- CI: Linux, `tests/run-all.sh`, the contract test, and a "works alone" job with no lean-agent-tools anywhere: install, `add`, `list`, `digest`, MCP `initialize` and `tools/list`.

## Tests in lean-agent-tools

- The queue and flaky self-tests move from `lib/brain/selftest.py` to the new package's self-test.
- Tests that file into brain use a fake `brain` on PATH (`wait-loop.py` already does this).
- One CI job checks out brain at a pinned tag and runs test-phase end to end against it.
- One CI job proves "works without brain": test-phase drains a fixture job with no brain on PATH and prints the failure.

## Release and the switch

1. Build both repos to green CI.
2. Release brain 1.0.0 and lean-agent-tools 2.0.0 the same day. lean-agent-tools' release notes and README lead with "install brain for failures and questions".
3. Arnel's machine, in one sitting, with the queue empty: add the brain marketplace, install brain, update lean-agent-tools, do the parked lean-agent 2.0.0 update (brain #10503), restart. Installing brain before updating lean-agent-tools would start two brain MCP servers, so both happen before the restart.
4. Update CLAUDE.md, the tools skill and the memory notes.

## Team sync later

What this change does to keep the door open:

- Everything left in `brain.db` is syncable except `sessions`, which the team-brain spec already marks local only.
- The Oct 20 team-brain spec says the team shares "queue jobs" through brain sync. After this split a team queue belongs to lean-agent-tools, which would talk to the same hosted service on its own. That line changes when the spec moves to the brain repo.
- No event log or UUIDs now. The team-brain spec adds them in its own step.

## Decided by Arnel (Nov 21 2026)

- **Name: `brain`.** Repo `arnelirobles/brain` (free as of Nov 21 2026), plugin `brain`, marketplace `brain`, so it installs as `brain@brain`. MCP tools become `mcp__plugin_brain_brain__brain_*`. The plugin lookup is `~/.claude/plugins/cache/*/brain/*/tools/brain`.
- **Owner and visibility: arnelirobles, public.** lean-agent-tools CI checks it out at a pinned tag with no token.
- **License: MPL-2.0 for the whole repo**, the extension included. Versions already published stay MIT.

Following from the name (my calls): the VS Code extension keeps its Marketplace id `baryo-dev.lean-brain` so installs carry over. `LEAN_BRAIN_DB`, `LEAN_BRAIN_RESUME`, `LEAN_SESSION_*` and `~/.local/share/lean/brain.db` keep their names, with no aliases needed.

## Left to the builder

- Name of the queue package inside lean-agent-tools.
- How `lib/brain/selftest.py` (1,446 lines) splits between the two repos.
- The exact `--json` shape of `brain list`, as long as test-phase and the contract test agree.
