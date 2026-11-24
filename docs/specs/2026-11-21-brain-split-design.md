# Brain, lean-agent-tools and the VS Code extension as three projects

Date: 2026-11-21. Status: approved by Arnel Nov 21 2026 as a two-way split; revised the same day to three projects, waiting on his review of the revision.

## Why

Brain is a local list of what went wrong and is not fixed yet, shared by every session on the machine: test failures, blockers, questions for a person, rulings, notes. Today it ships inside lean-agent-tools, so only lean-agent users get it, and it carries the test queue that only lean-agent's `test-phase` uses.

Arnel wants brain for anyone: it installs and works without lean-agent, and team sync gets added later.

He also wants each piece separate, and the VS Code extension to need the whole set. In his words: "each one is separate and for vs extension it needs whole so it needs to install it if i install vscode".

This is the second split. The first (Nov 7 2026) moved everything that is not Markdown out of lean-agent into lean-agent-tools. This one makes three projects out of lean-agent-tools:

| Project | Repo | What it is |
|---|---|---|
| brain | `arnelirobles/brain` | items, the `brain` command, its MCP server, the brain page, its own hooks |
| lean-agent-tools | `arnelirobles/lean-agent-tools` | hooks, scripts, test-phase, and now the test queue and run history |
| Lean Brain extension | `arnelirobles/lean-agent-vscode` | the VS Code extension, showing brain items and the test queue |

## Goals

- Brain installs and works alone: the `brain` command, the MCP server, the brain page, and brain's own Claude Code hooks (session id on items, digest at start and after a compaction, answers handed back to the session that asked).
- lean-agent-tools works without brain. With brain installed, test failures, blockers and hook notices land in brain as they do today.
- The VS Code extension keeps every view it has today (open items, queue, lanes, runs). Installing it gets the user lean-agent, lean-agent-tools and brain: it finds the missing ones and installs them on one click.
- The only contract between brain and lean-agent-tools is the `brain` command line. The extension reads both databases and calls both commands. Each repo versions and releases on its own.
- No data is lost. Items, sessions, created records and notices stay in `brain.db`. Queue and run history move to lean-agent-tools' own database.

## Non-goals

- Team sync. This change only keeps the door open (see Team sync later).
- PyPI, Homebrew or npm packaging.
- Changing what an item is, its kinds, or how the brain page looks beyond removing its queue and run sections. The extension is where queue and runs are watched.
- PR #126 in arnelirobles/lean-agent (the brain assistant). It gets rebuilt on the brain repo later.

## Anti-goals

These would make it a failure even if it runs:

- Brain knowing about tests. After the split, the brain repo has no `jobs` or `runs` table code, no test project, lane, filter or dotnet logic. The one exception is the one-version forward of `brain queue` (see Compatibility).
- lean-agent-tools importing brain's Python or opening `brain.db`. The one exception is the one-time copy of the queue history (see Moving the data).
- The extension installing anything without a click.
- A switch that leaves Arnel's machine running two brain MCP servers, or losing queued jobs mid-batch.

## Approaches

- **A. Brain keeps the queue, runs and flaky tracking; lean-agent-tools requires brain.** Smallest move and no command changes. Rejected: anyone installing brain for Codex or Cursor gets test commands they can never use, team sync has to carve tables out, and lean-agent-tools stops working without brain.
- **B. Brain becomes a plain item store; the queue, runs and flaky tracking move to lean-agent-tools with test-phase.** Brain is generic and fully syncable, and each plugin works alone. Costs a command rename (`brain queue add` becomes `test-phase queue add`) and a one-time data move. **Picked by Arnel.**
- **C. Brain takes test-phase too.** Rejected: brain would carry a 4,500-line dotnet, vitest and gradle runner nobody outside lean-agent needs.

After B was picked, Arnel moved the extension out to its own repo, needing all three plugins (Nov 21 2026).

## What moves where

| Piece (today in lean-agent-tools) | After |
|---|---|
| `lib/brain/store.py`, `items.py`, `records.py`, `activity.py` | brain repo |
| `lib/brain/report.py` | brain repo, minus the job and run parts of `digest`, `resume` and `stats` |
| `lib/brain/cli.py` | brain repo, minus `queue`, `run-add`, `flaky-clear` |
| `lib/brain/web/*` (the brain page) | brain repo, minus the queue and run sections |
| `tools/brain`, `tools/brain-mcp` | brain repo, `brain-mcp` minus the `brain_queue_*` tools |
| `hooks/brain-resume.py`, `hooks/serve-start.py`, the brain half of `hooks/session-env.py` (session start and end, answers on prompt) | brain repo, as brain's own hooks |
| `docs/specs/2026-10-20-team-brain-design.md`, `docs/specs/2026-10-24-brain-web-page-design.md`, `docs/plans/2026-10-24-brain-web-page.md` | brain repo |
| `plugin.json` `mcpServers.brain`, `userConfig.serve_mode`, `userConfig.worktree_roots` | brain's `plugin.json` |
| `vscode/` (the Lean Brain extension) and the `vscode` CI job | `arnelirobles/lean-agent-vscode`, every view kept. Closes lean-agent #143 |
| `lib/brain/queue.py`, `lib/brain/flaky.py`, the `jobs` and `runs` tables | lean-agent-tools, new package beside test-phase (name is the builder's) |
| `test-phase`, `holdout.sh`, `lean-stats`, every other hook and tool | stay in lean-agent-tools |
| `session-env.py` PATH line and the `lean-stats collect` on SessionEnd | stay in lean-agent-tools |

## The contract between brain and lean-agent-tools

lean-agent-tools reaches brain only by running it.

- **Finding brain** (my call): `command -v brain` first, then the newest `~/.claude/plugins/cache/*/brain/*/tools/brain`. `hookkit.tool_path("brain")` changes to this lookup. Not found: skip quietly, and test-phase prints each failure in its log instead of filing it.
- **Too old a brain** (my call): brain 1.0 adds `brain --version`, printing `brain 1.0.0`. lean-agent-tools runs it once per process and needs 1.0 or newer. A brain that fails it (the 1.x copy inside lean-agent-tools has no `--version`) counts as not found, with one stderr line naming the path it found.
- **Calls lean-agent-tools makes after the split**, the whole contract:
  - `brain add` with the body on stdin, `brain list --json` (with `--open --kind --project --tag --cycle`), `brain close ID --resolution --note`. These are the item calls `tools/test-phase` makes today.
  - `brain notice add` and `brain created add`, new, replacing the direct imports in `lib/hookkit.py` and `hooks/created-log.py`.
  - `brain session infer --json`, new (see The queue in lean-agent-tools).
  - The queue calls test-phase makes to brain today (`queue requeue|next|finish|cancel|list|route`, `run-add`, `flaky-clear`) become calls into its own queue package. They leave the contract.
- **Notices and created records** (my call): run with a 1 second timeout, as today. On a timeout, a missing brain or an error, the record is dropped and the hook goes on. The hookkit self-test "locked brain returns within 2 s" stays.
- **Parsed output** (my call): add `brain list --json` and have test-phase read that, instead of matching `^#\d+ open` lines across two repos. Each item carries at least `id`, `kind`, `status`, `project`, `repo`, `branch`, `cycle`, `run_id`, `ref`, `title`, `tags`, `first_seen`, `last_seen`.
- **Contract test**: the brain repo keeps a list of every call lean-agent-tools and the extension make and runs each against a scratch database in CI. A change that breaks one fails brain's CI, not Arnel's next batch.

## The queue in lean-agent-tools

- Own database (my call): `~/.local/share/lean/tests.db`, overridable with `LEAN_TESTS_DB`, holding `jobs` and `runs` with today's columns and ids, plus a `meta` table with `schema_version` like `brain.db` has, for the extension to check.
- Commands (my call): `test-phase queue add|list|wait|cancel|bump|move|next|route|requeue|finish`, `test-phase run-add`, `test-phase flaky-clear`, `test-phase stats`. Plain `test-phase` still drains. This keeps Arnel's rule of one drainer under one name.
- MCP (my call): lean-agent-tools ships a small MCP server named `tests` with the queue tools (`queue_add`, `queue_list`, `queue_bump`, `queue_cancel`, `queue_route`). `queue next` stays drainer only, as `tools/brain-mcp:599` tests today. The JSON-RPC loop is copied from `brain-mcp`, not shared.
- Session on a job (my call): `jobs` keeps `session_id` and `session_cwd`. The queue takes them from `$LEAN_SESSION_ID` and `$LEAN_SESSION_CWD`. When those are unset and brain is found, it calls `brain session infer --json`, which prints the one open session started in the current folder (today's `infer_session` in `lib/brain/items.py`), or empty when there is none or more than one. Without brain, the job has no session.
- Digest after a compaction (my call): `brain digest` today lists queued and running jobs. lean-agent-tools prints those lines in its own SessionStart note (startup and compact), as a separate block with its own cap, not inside brain's digest budget.
- Items keep their `cycle` column, so `brain list --cycle C` still lists a cycle's failures. `items.run_id` stays as a plain number with no reference to a table brain no longer has (SQLite never enforced it here).
- `brain stats` keeps its item parts: counts by kind and status, open by project, stale agents, median time to close. The suites and slowest-runs parts move to `test-phase stats` (my call). The `brain_stats` MCP tool stays with the item parts.
- Flaky tracking and `test-phase stats` (my call) keep their flaky columns. They read runs from their own database and `test-fail` and `flaky` items from `brain list --json`, grouped by `run_id`. Without brain those columns read "brain not installed".
- The drainer lock stays at `~/.local/share/lean/test-phase/drainer.lock` (`tools/test-phase:2151`), so a 1.x drainer still running blocks a 2.0.0 one until it exits.

## The VS Code extension

- Repo `arnelirobles/lean-agent-vscode`, public, MPL-2.0. Marketplace id stays `baryo-dev.lean-brain` and the display name stays "Lean Brain", so installs carry over (my call).
- **Needs all three plugins:** `lean-agent@lean-agent`, `lean-agent-tools@lean-agent-tools` and `brain@brain`.
- **Installing them** (my call): on activation it runs `claude plugin list --json`. When any of the three is missing, it shows one notice naming the missing ones, with Install and Not now. Install opens a terminal named "Lean Brain setup" that runs, for each missing plugin, `claude plugin marketplace add arnelirobles/<repo>` (skipped when `claude plugin marketplace list` already has it) and `claude plugin install <id> --scope user`, then says to restart open Claude Code sessions. Nothing installs without the click, per Arnel's Oct 21 2026 rule that nothing installs without a yes. Not now asks again on the next activation. Without `claude` on PATH, the notice shows the commands to copy instead.
- **Two databases** (my call): it opens `brain.db` (items, tags, sessions, meta) and `tests.db` (jobs, runs, meta) as two sql.js databases. `LEAN_TESTS_DB` and a new setting beside the brain database setting in `vscode/src/brainCli.ts:9` give the second path. The flaky-per-cycle count at `vscode/src/queries.ts:57` joins `runs` to `items`; it becomes two reads joined in TypeScript.
- **A missing database** (my call): each view works from the data that exists. A view whose database is missing shows one line naming the plugin to install.
- **Commands it runs:** queue actions (the `queue move`, `queue route` and `queue --help` calls in `vscode/src/brainCli.ts:87-99`) call `test-phase queue ...`. Item actions keep calling `brain`.
- **Schema check** (my call): it reads `schema_version` from both databases and, when one is newer than it knows, names that database and says to update the extension.
- **The deprecation notice goes** (my call): `vscode/src/deprecation.ts` (Oct 24 2026) tells users the panel will be retired and to open the brain page. With the extension as its own project, that notice and its setting key are removed. The Open brain page command stays.
- **The schema test** (`vscode/test/schema.test.ts`) reads `lib/brain/*.py` from the same checkout. In its own repo that source is gone, so the fixture job below replaces it.
- **Older builds** (my call): none of the 0.x builds get a compatibility path. `package.json` keeps `publisher` and `name`; `repository`, `license` and `version` change.

## Brain's hooks without hookkit

`lib/hookkit.py` is lean-agent-tools' shared hook library (1,972 lines with its self-tests). Brain's three hooks need only: read the event JSON, add context, append to `$CLAUDE_ENV_FILE`, and call brain's own library directly (today they run `brain session start|end` and `brain answers` as subprocesses). Brain gets its own small hook helper and a hand-written `hooks.json` (my call), not a copy of hookkit or the HOOK-literal build.

- Events, matchers and timeouts are a straight port of today's entries in `hooks/hooks.json` and `hooks/dispatch.json` (my call). `serve_mode` stays `manual` by default.
- Brain's SessionStart hook writes `LEAN_SESSION_ID`, `LEAN_SESSION_CWD` and brain's tools folder on PATH to `$CLAUDE_ENV_FILE`. lean-agent-tools' `session-env.py` keeps its own `LEAN_SESSION_*` and PATH lines, since its queue needs the session without brain.
- Each plugin appends only its own lines, under its own copy of the file lock in `session-env.py:120` (my call). Which plugin runs first does not matter: the session lines carry the same value and each PATH line adds a different folder. The order test at `session-env.py:253` splits into one test per side that checks only that side's lines.
- Switches keep their names: `LEAN_BRAIN_DB`, `LEAN_BRAIN_RESUME`, `LEAN_SESSION_ENV`. `LEAN_PROFILE=off` also turns brain's hooks off, so one switch still silences every hook (my call). Each brain hook catches every error and exits 0, as `hookkit.run` does today.
- `serve_mode` and `worktree_roots` are read only by the brain page (`hooks/serve-start.py`, `lib/brain/cli.py:385`), so both move whole.

## Other tools (Codex, Cursor)

They get the CLI and the MCP server, no hooks. The README shows the MCP config pointing at a clone's `tools/brain-mcp`. Answers handed back to the asking session stay Claude Code only in this version.

## Compatibility

- `brain queue ...`, `brain run-add` and `brain flaky-clear` in brain 1.0 forward to `test-phase` when it is found, with one stderr line saying the command moved. Without test-phase they exit 2 with that line. Removed in brain 1.1.
- MCP tool names change. `mcp__plugin_lean-agent-tools_brain__brain_add` becomes `mcp__plugin_brain_brain__brain_add`, and the queue tools become `mcp__plugin_lean-agent-tools_tests__queue_*`. Sessions read tool names at start, so only written text needs updating.
- Text that names `brain queue add` and must change: `skills/tools/SKILL.md`, the messages in `hooks/script-note.py` and `hooks/wait-loop.py` and their self-tests, Arnel's `~/.claude/CLAUDE.md` ("Tests and open items"), and the memory notes that quote it. Arnel approves the CLAUDE.md wording.
- `brain.db` stays at `~/.local/share/lean/brain.db`.
- Versions: lean-agent-tools goes to 2.0.0 (the queue command and MCP names break). Brain starts at 1.0.0. The extension goes to 1.0.0 (it now needs three plugins and reads two databases).

## Moving the data

- On first open of `tests.db`, when it is empty and `brain.db` has `jobs` or `runs`, lean-agent-tools copies both tables, ids kept. This is the one read of `brain.db` it ever makes, through a read-only SQLite open (`mode=ro`). `brain export` cannot do it: it covers only `items` and `tags` (`lib/brain/report.py:225-250`). The copy goes away in lean-agent-tools 2.1.
- The copy refuses while `brain.db` has queued or running jobs (my call). It prints how many and says to drain them with lean-agent-tools 1.x first, and every queue command exits 2 until then. Nothing is copied halfway.
- The copy goes by column: each column `tests.db` has that the old table also has is copied, the rest take their defaults (my call). A `brain.db` that stopped at an older migration still copies, and nothing has to migrate it first.
- Brain never drops `jobs` or `runs` from an existing database. New databases do not get them (my call on how): the schema version stays 9, so a 1.x drainer can still open the file to drain it before the switch. `migrate_2` creates `items` and `tags` only, with `items.run_id` a plain integer; `migrate_3`, `4`, `6`, `7` and `8`, which touch only `jobs` and `runs`, do nothing; `migrate_9`, `ensure_extras` and the check in `connect` skip `jobs` and `runs` when the table is missing. An old file keeps its tables and its version.
- The switch happens with an empty queue and no drainer running.

## Building the new repos

- Both are made from lean-agent-tools with history kept for their own paths only (`git filter-repo --path ...`, my call), so blame and log survive. Before either is pushed, `tools/secret-scan` runs over the split history (a lean-agent #143 constraint, applied to both).
- License MPL-2.0 for both (Decided by Arnel). The extension's `package.json` and `vscode/LICENSE` say MIT and `package.json` points at the old lean-agent repo; all three get corrected.
- Brain CI: Linux, `tests/run-all.sh`, the contract test, and a "works alone" job with no lean-agent-tools anywhere: install, `add`, `list`, `digest`, MCP `initialize` and `tools/list`.
- Extension CI: typecheck and vitest, as the `vscode` job does today, plus one job that builds `brain.db` and `tests.db` by running `brain` and `test-phase` at pinned tags and runs the extension's queries against them.

## Tests in lean-agent-tools

- The queue and flaky self-tests move from `lib/brain/selftest.py` to the new package's self-test.
- Tests that file into brain use a fake `brain` on PATH (`wait-loop.py` already does this).
- One CI job checks out brain at a pinned tag and runs test-phase end to end against it.
- One CI job proves "works without brain": test-phase drains a fixture job with no brain on PATH and prints the failure.

## Release and the switch

The build runs on local branches up to green tests. Creating the two GitHub repos, every push, every tag and release, the Marketplace publish and the CLAUDE.md wording each wait on Arnel's yes (my call, from his standing rules).

1. Build the three repos to green CI.
2. Release brain 1.0.0 and lean-agent-tools 2.0.0 the same day. lean-agent-tools' release notes and README lead with "install brain for failures and questions".
3. Arnel's machine, in one sitting, with the queue empty and no drainer running: add the brain marketplace, install brain, update lean-agent-tools, restart. Installing brain before updating lean-agent-tools would start two brain MCP servers, so both happen before the restart. `claude plugin list` already shows lean-agent 2.0.0 at user scope, so the rest of brain #10503 stays its own item.
4. Install the extension 1.0.0 from its `.vsix` in the same sitting, so the old build does not show a frozen queue from `brain.db`. Publishing it to the Marketplace needs a new token (the old one expired; the listing shows 0.5.1), which is Arnel's step.
5. Update CLAUDE.md, the tools skill and the memory notes.

## Team sync later

What this change does to keep the door open:

- Everything left in `brain.db` is syncable except `sessions`, which the team-brain spec already marks local only.
- The Oct 20 team-brain spec says the team shares "queue jobs" through brain sync. After this split a team queue belongs to lean-agent-tools, which would talk to the same hosted service on its own. That line changes when the spec moves to the brain repo.
- No event log or UUIDs now. The team-brain spec adds them in its own step.

## Decided by Arnel (Nov 21 2026)

- **Three separate projects**, the extension needing all of lean-agent, lean-agent-tools and brain, and installing the missing ones.
- **Brain's name: `brain`.** Repo `arnelirobles/brain` (free as of Nov 21 2026), plugin `brain`, marketplace `brain`, so it installs as `brain@brain`. MCP tools become `mcp__plugin_brain_brain__brain_*`. The plugin lookup is `~/.claude/plugins/cache/*/brain/*/tools/brain`.
- **Brain's owner and visibility: arnelirobles, public.** lean-agent-tools CI checks it out at a pinned tag with no token.
- **The extension's repo: `arnelirobles/lean-agent-vscode`**, public as lean-agent #143 asks.
- **License: MPL-2.0** for brain and the extension. Extension versions already published stay MIT.

Following from the names (my calls): `LEAN_BRAIN_DB`, `LEAN_BRAIN_RESUME`, `LEAN_SESSION_*` and `~/.local/share/lean/brain.db` keep their names, with no aliases needed.

## Left to the builder

- Name of the queue package inside lean-agent-tools.
- How `lib/brain/selftest.py` (1,446 lines) splits between the two repos.
- The exact `--json` shape of `brain list`, as long as test-phase, the extension and the contract test agree.
- How the extension's install notice and setup terminal look, within the rules above.
