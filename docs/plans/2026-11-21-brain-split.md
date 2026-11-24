# Brain Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three projects out of lean-agent-tools: `brain` (items, CLI, MCP server, page, its own hooks), lean-agent-tools 2.0.0 (hooks, scripts, test-phase, and now the test queue in `tests.db`), and `lean-agent-vscode` (the Lean Brain extension, reading both databases and offering to install the plugins it needs).

**Architecture:** Part A does every code move inside lean-agent-tools on branch `brain-split`, so one test suite stays green while brain loses its test code and lean-agent-tools stops importing brain. Part B cuts the brain repo from that branch with `git filter-repo`. Part C deletes brain from lean-agent-tools. Part D cuts and reworks the extension. Part E is the release and the switch, each step on Arnel's yes. The only link between brain and lean-agent-tools is the `brain` command, reached through one module, `lib/brainlink.py`.

**Tech Stack:** Python 3 standard library (`sqlite3`, `argparse`, `subprocess`, `fcntl`), bash 3.2 compatible shell, `git filter-repo`, TypeScript with sql.js, esbuild and vitest for the extension, the `claude plugin` CLI.

**Spec:** `docs/specs/2026-11-21-brain-split-design.md`

## Global Constraints

- Python standard library only. Run Python tools with `sys.executable`, never a literal `python3`.
- `brain.db` stays at `LEAN_BRAIN_DB` or `~/.local/share/lean/brain.db`, schema version 9. `tests.db` is at `LEAN_TESTS_DB` or `~/.local/share/lean/tests.db`, schema version 1. The drainer lock stays at `<LEAN_TEST_PHASE_DIR or ~/.local/share/lean/test-phase>/drainer.lock`.
- Versions: brain `1.0.0`, lean-agent-tools `2.0.0`, extension `1.0.0`. Marketplace id stays `baryo-dev.lean-brain`.
- MCP: brain's server is `brain` (tools `brain_add`, `brain_close`, `brain_digest`, `brain_list`, `brain_show`, `brain_stats`). lean-agent-tools' server is `tests` (tools `queue_add`, `queue_list`, `queue_bump`, `queue_cancel`, `queue_route`). `queue next` is never exposed.
- License MPL-2.0 for brain and the extension.
- Brain's code never names `jobs`, `runs`, lanes, test projects or dotnet, except in `lib/brain/store.py` migrations and `lib/brain/forward.py`. lean-agent-tools never imports `brain` or opens `brain.db`, except the read-only copy in `lib/testq/store.py`.
- Every new script or `lib/` module with logic has a `--self-test` that `tests/run-all.sh` finds. `bash tests/run-all.sh` ends with `0 failed`.
- Run steps: one self-test runs directly (`python3 <file> --self-test`). The whole suite runs as `bash tools/heavy.sh python bash tests/run-all.sh`; vitest as `bash tools/heavy.sh node npx vitest run`. No sleep or pgrep wait loops. Coding agents build only; the coordinator runs the run steps.
- No key or token in any repo. Before a cut repo is pushed, `tools/secret-scan scan --repo <dir> --range HEAD` scans its history (a bare `HEAD` runs `git log -p` over every commit, the root included), and no `.vsix` is in any history.
- Each cut starts from a one-branch clone of the local branch `split-base` (made at the end of Task 9) with `--single-branch --no-tags`, so no other branch or lean-agent-tools tag reaches a new repo.
- No GitHub repo created, nothing pushed, tagged, released or published without Arnel's yes. PRs stay drafts until review is done and checks pass.
- Commit messages: short, imperative, plain words, no trailers. No em dashes, arrow characters or the banned words in any text. Run `style-scan` before each commit.
- Never touch `~/repos/lean-agent` (stale) and never add scripts to lean-agent, which is Markdown only.

## Review Focus

1. **The switch with a 1.x drainer still alive or jobs still queued in `brain.db`.** Expect the 2.0.0 drainer to wait on the same lock and every queue command to exit 2 naming the count, with nothing copied. Pinned in Task 3 and Task 5.
2. **A locked `brain.db` while a hook records a notice.** Expect the hook to finish within 2 s with its output unchanged. Pinned in Task 6.
3. **Both plugins writing `$CLAUDE_ENV_FILE` at one SessionStart, in either order, then again on resume.** Expect each `LEAN_SESSION_*` line once and one PATH line per plugin. Pinned in Task 9.
4. **An old `brain` on PATH from a 1.x env file (no `--version`).** Expect it skipped with one stderr line and the newer cached brain used, or failures printed in the job log when there is none. Pinned in Task 2 and Task 5.
5. **The extension with `tests.db` missing, or `claude` not on the editor's PATH.** Expect the items view to work, the queue view to name lean-agent-tools, and the install notice to offer the commands to copy. Pinned in Task 13 and Task 15.

---

## Part A: the moves, inside lean-agent-tools (branch `brain-split`)

### Task 1: Brain's new contract calls

**Files:**
- Create: `lib/brain/version.py` (`VERSION = "1.0.0"`)
- Modify: `lib/brain/cli.py` (parsers), `lib/brain/items.py` (`cmd_list`, `cmd_session`), `lib/brain/records.py` (`cmd_notice`, `cmd_created`)
- Test: `lib/brain/selftest.py`

**Interfaces:**
- Produces: `brain --version` prints `brain 1.0.0`. `brain list --json [--open --kind --project --tag --cycle --status]` prints a JSON array, same rows and order as the text list, each object with `id kind status project repo branch cycle run_id ref title tags first_seen last_seen` (`tags` a sorted list). `brain notice add --hook H [--session S] [--cwd C]` reads the text on stdin and prints the notice id. `brain created add --kind K --forge F --url U [--repo --branch --number --title --session --cwd]` prints the id. `brain session infer --json` prints `{"id": "...", "cwd": "..."}`, both empty when there is no single open session for the folder.

- [ ] **Step 1: Write failing checks** in `selftest.py`: `--version` output is `brain 1.0.0`; `list --json --kind test-fail` on two seeded items returns two objects with exactly the 13 keys and `tags` sorted; `list --json` on an empty DB prints `[]`; `notice add` twice with the same hook and text prints the same id (10-minute window); `notice add` with empty stdin exits 2; `created add` twice with one URL prints the same id; `session infer --json` with one open session started in the cwd returns it, with two returns empty ids and one stderr line.
- [ ] **Step 2: Run** `python3 tools/brain --self-test`. Expected: FAIL on the `--version` check.
- [ ] **Step 3: Implement** the parsers and handlers. `notice add` and `created add` call `records.add_notice` and `records.add_created`; `session infer` wraps `items.infer_session`.
- [ ] **Step 4: Run** `python3 tools/brain --self-test`. Expected: `brain self-test: ok`.
- [ ] **Step 5: Commit** `git commit -m "add brain --version, list --json, notice add, created add and session infer"`

### Task 2: One module that finds and runs brain

**Files:**
- Create: `lib/brainlink.py`, `lib/testq/fakebrain.py`

**Interfaces:**
- Produces, in `lib/brainlink.py`: `MIN_VERSION = (1, 0, 0)`; `find_brain(env: dict | None = None, home: str | None = None) -> str | None`; `call(args: list[str], stdin: str | None = None, timeout: float | None = None) -> str | None`; `list_json(args: list[str]) -> list[dict] | None`; `reset() -> None` (clears the per-process cache, for tests).
- Produces, in `lib/testq/fakebrain.py`: a stand-in `brain` for tests. It answers `--version` (from `$FAKE_BRAIN_VERSION`, default `brain 1.0.0`), `add`, `list --json`, `close` and `session infer --json` from a JSON file at `$FAKE_BRAIN_STATE`, appends each argv as a JSON line to `$FAKE_BRAIN_LOG`, and sleeps `$FAKE_BRAIN_SLEEP` seconds first when set. `install_fake(dir: str, env: dict) -> str` writes an executable `brain` into `dir` and returns its path.

- [ ] **Step 1: Write failing checks** (`python3 lib/brainlink.py --self-test`): a fake brain on PATH is found; an old one on PATH (`--version` exits 2) is skipped with one stderr line naming its path and a fake at `HOME/.claude/plugins/cache/m/brain/1.0.0/tools/brain` is used instead; cache versions `1.2.0` and `1.10.0` pick `1.10.0`; nothing anywhere returns None and prints nothing; `call` with `timeout=1` on a fake sleeping 5 s returns None in under 2 s; `list_json` on output that is not JSON returns None.
- [ ] **Step 2: Run** the self-test. Expected: FAIL, module missing.
- [ ] **Step 3: Implement.** Candidates in order: `shutil.which("brain")`, then `glob(home/.claude/plugins/cache/*/brain/*/tools/brain)` sorted by the version folder as a number tuple, newest first. A candidate counts when `[sys.executable, path, "--version"]` prints `brain X.Y.Z` at least `MIN_VERSION` within 5 s. The first hit is cached for the process. `call` returns None and writes `lean-agent-tools: brain failed: <stderr>` on a non-zero exit.
- [ ] **Step 4: Run** the self-test. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "find and run brain through one module"`

### Task 3: `tests.db` and the one-time copy

**Files:**
- Create: `lib/testq/__init__.py`, `lib/testq/store.py`, `lib/testq/selftest.py`
- Modify: `tests/run-all.sh` (add `lib/testq/*.py` to the `--self-test` glob)

**Interfaces:**
- Produces: `SCHEMA_VERSION = 1`; `db_path() -> str`; `brain_db_path() -> str`; `class CopyRefused(Exception)` with `.count: int`; `connect(timeout: float = 30) -> sqlite3.Connection` (row factory `sqlite3.Row`, `with con:` opens a transaction, as `lib/brain/store.py:209` does); `copy_from_brain(con, src: str) -> int`.

- [ ] **Step 1: Write failing checks** in `lib/testq/selftest.py`: a fresh `tests.db` has `jobs`, `runs` and `meta` with `schema_version` `1`, and the `jobs` and `runs` columns equal those of a brain.db built by today's `lib/brain/store.py` (including `branch`); a version 9 brain.db with 3 jobs (all done) and 2 runs copies all 5 rows with ids kept, and its bytes are unchanged (sha256 before and after); a version 4 brain.db (no `priority`, `platform`, `host`, `commit_hash`, `session_id`, `branch`) copies with those columns at their defaults; a brain.db with 1 queued and 1 running job raises `CopyRefused` with `count == 2` and leaves `tests.db` empty, and after both are marked done the next `connect` copies; a missing brain.db gives an empty `tests.db`, and a later brain.db with jobs is not copied (the copy runs once).
- [ ] **Step 2: Run** `python3 lib/testq/selftest.py --self-test`. Expected: FAIL, module missing.
- [ ] **Step 3: Implement.** `connect` creates the tables, then calls `copy_from_brain(con, brain_db_path())` unless `meta` has `copied_from_brain`. The copy opens `file:<src>?mode=ro` as a URI, counts `jobs` with status `queued` or `running` and raises when any, else inserts the shared columns of each table (`PRAGMA table_info` on both sides) and writes the `copied_from_brain` timestamp, all in one transaction. A missing file or missing tables only writes the marker.
- [ ] **Step 4: Run** the self-test. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "give the test queue its own database and copy the old history once"`

### Task 4: Queue, run, flaky and stats commands in `test-phase`

**Files:**
- Create: `lib/testq/queue.py` (copied from `lib/brain/queue.py`), `lib/testq/flaky.py` (from `lib/brain/flaky.py`), `lib/testq/runs.py` (from `cmd_run_add`, `lib/brain/items.py:202-214`), `lib/testq/stats.py` (suites and slowest from `lib/brain/report.py:180-191,215-221`), `lib/testq/cli.py`
- Modify: `tools/test-phase:2117` (`main` dispatch), `lib/testq/selftest.py` (the queue and flaky checks copied from `lib/brain/selftest.py`, pointed at `tests.db`)

Brain keeps its own copies until Task 8 deletes them.

**Interfaces:**
- Consumes: `testq.store.connect`, `CopyRefused`; `brainlink.call`, `brainlink.list_json`; `fakebrain.install_fake`.
- Produces: `testq.cli.main(argv: list[str]) -> int` for `queue add|list|wait|cancel|bump|move|next|route|requeue|finish`, `run-add`, `flaky-clear`, `stats [--json]`, flags and output text as `brain` has them today; `testq.queue.job_session() -> tuple[str, str]`; `test-phase queue|run-add|flaky-clear|stats ...` calls `testq.cli.main` before the drain parser. A plan file named `queue` has to be passed as `./queue`.

- [ ] **Step 1: Write failing checks**, besides the copied ones: `job_session()` returns `$LEAN_SESSION_ID`/`$LEAN_SESSION_CWD` when set; when unset with a fake brain it returns what `session infer --json` gave; with no brain it returns `("", "")`. `flaky-clear` closes a flaky item through `brain close ... --resolution flaky-cleared` (seen in the fake's argv log) after 5 clean runs in `tests.db`. `stats --json` has `suites` and `slowest`, with `flaky_items` counted from the fake's `list --json --kind flaky` items (every status) matched to runs by `run_id`, and `flaky_rate` as `lib/brain/report.py:186-187` computes it; with no brain each suite's `flaky_items` and `flaky_rate` are `"brain not installed"`. Any queue command while `CopyRefused` exits 2 with `test-phase: brain.db still has 2 queued or running jobs. Drain them with lean-agent-tools 1.x, then run this again.`
- [ ] **Step 2: Run** `python3 lib/testq/selftest.py --self-test`. Expected: FAIL on the first new check.
- [ ] **Step 3: Implement.** `job_session` replaces the `infer_session` import (`lib/brain/queue.py:14,281-283`); copy the three-line `session_ok` check. The help text is `lib/brain/cli.py:10-47,101-104` with `brain queue` written as `test-phase queue`.
- [ ] **Step 4: Run** the self-test and `python3 tools/test-phase --self-test`. Expected: both pass.
- [ ] **Step 5: Commit** `git commit -m "move the queue, run and flaky commands into test-phase"`

### Task 5: test-phase drains through its own queue and files through brainlink

**Files:**
- Modify: `tools/test-phase` (`BRAIN` at 181, `brain()` at 1195, item calls at 1214, 1294, 1300, 1311, 1357, 1399, queue calls at 1206, 1231, 1822, 1876, 1896-2203, self-test calls at 2583, 3688, 4145, 4204)

**Interfaces:**
- Consumes: `testq.cli.main`, `brainlink.call`, `brainlink.list_json`, `fakebrain.install_fake`.
- Produces: `queue_cli(args: list[str]) -> str | None` in `tools/test-phase`, which runs `testq.cli.main(["queue", *args])` in process with stdout captured and returns None on a non-zero code, so the existing `^job #` regexes keep working.

- [ ] **Step 1: Write failing checks** in the test-phase self-test: with no brain on PATH and an empty `HOME`, a fixture job with one failing test drains, the job ends `failed` in `tests.db`, and the job log has `brain not installed, not filed:` followed by the failing test's name; with a fake brain, the failure is filed as `add --kind test-fail` and the blocker lookups use `list --json` (both in the argv log) and the log has no `queue` call to brain at all; with an old brain on PATH and no cache, the same "not filed" path runs and stderr has the one skipped-brain line; a second drainer started while the first holds `<LEAN_TEST_PHASE_DIR>/drainer.lock` exits without taking a job.
- [ ] **Step 2: Run** `python3 tools/test-phase --self-test`. Expected: FAIL on the no-brain check.
- [ ] **Step 3: Implement.** Queue calls go through `queue_cli`; `run-add` and `flaky-clear` call `testq` directly; item calls go through `brainlink`, and the `^#\d+ open` parsing at 1294, 1311 and 1357 reads `list_json` instead. When `brainlink.call` returns None for an `add`, write the item's kind, ref, title and the first 40 body lines to the job log under the "not filed" line. Self-test calls at 2583, 3688, 4145 and 4204 run `test-phase queue add`.
- [ ] **Step 4: Run** `python3 tools/test-phase --self-test`. Expected: pass within the 900 s cap.
- [ ] **Step 5: Commit** `git commit -m "drain from the test queue and file failures through the brain command"`

### Task 6: Hook notices and created records through the command

**Files:**
- Modify: `lib/hookkit.py:640-652` (`_keep_notice`), `lib/hookkit.py:1910-1937` (the locked-brain self-test), `hooks/created-log.py:148-151,225-232`

**Interfaces:**
- Consumes: `brainlink.call`, `fakebrain.install_fake`.

- [ ] **Step 1: Write failing checks**: `hookkit.note` with a fake brain records `notice add --hook <name> --session <sid> --cwd <cwd>` with the text on stdin; with a fake sleeping 5 s the note returns within 2 s and prints the same output as with no brain; `LEAN_NOTICES=off` makes no brain call. `created-log.py` on `gh pr create` output records one `created add --kind pr --forge github --url https://github.com/o/r/pull/12 --number 12` call, and a second identical run still prints nothing extra.
- [ ] **Step 2: Run** `python3 lib/hookkit.py --self-test` and `python3 hooks/created-log.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement** with `brainlink.call(..., timeout=1)`. A None result is dropped silently.
- [ ] **Step 4: Run** both self-tests. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "record notices and created items through the brain command"`

### Task 7: The `tests` MCP server and the queue note at session start

**Files:**
- Create: `tools/tests-mcp` (JSON-RPC loop copied from `tools/brain-mcp:290-376`, queue tools from `tools/brain-mcp:265-279`), `hooks/queue-note.py`
- Modify: `.claude-plugin/plugin.json` (`mcpServers.tests`: `${CLAUDE_PLUGIN_ROOT}/tools/tests-mcp`), `hooks/hooks.json` and `hooks/dispatch.json` (regenerated by `tools/lean-agent build`)

**Interfaces:**
- Consumes: `testq.cli.main` through `tools/test-phase`.
- Produces: tools `queue_add`, `queue_list`, `queue_bump`, `queue_cancel`, `queue_route` with today's schemas. `queue-note.py` HOOK: SessionStart, matcher `startup|compact`, timeout 10, pack `core`, default note in strict and standard.

- [ ] **Step 1: Write failing checks**: `tools/list` returns exactly the five names; `queue_next` and `brain_queue_add` get a JSON-RPC error; `queue_add` without `repo_dir` is `isError`. The queue note prints nothing for an empty queue; with 10 running jobs it prints 8 job lines and `+2 more running jobs` in 1,000 bytes or less; on `compact` it also lists queued jobs, 2,000 bytes or less; with `tests.db` locked it prints nothing and exits 0 within the timeout.
- [ ] **Step 2: Run** `python3 tools/tests-mcp --self-test` and `python3 hooks/queue-note.py --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement**, then run `tools/lean-agent build`.
- [ ] **Step 4: Run** both self-tests and `bash tools/heavy.sh python bash tests/run-all.sh`. Expected: `0 failed`, including the duplicate-key check on `hooks/hooks.json` and `.claude-plugin/*.json`.
- [ ] **Step 5: Commit** `git commit -m "serve the queue tools from lean-agent-tools and list jobs at session start"`

### Task 8: Brain without jobs and runs

**Files:**
- Create: `lib/brain/forward.py`
- Delete: `lib/brain/queue.py`, `lib/brain/flaky.py`
- Modify: `lib/brain/store.py` (migrations, `ensure_extras`, `connect`, plus a copy of `project_of`), `lib/brain/items.py` (drop `cmd_run_add`), `lib/brain/report.py` (job lines at 28-46, 86-93, 126; suites and slowest at 180-191, 215-221; import at 8), `lib/brain/cli.py` (forward `queue`, `run-add`, `flaky-clear`; help text), `tools/brain-mcp` (drop the `brain_queue_*` tools and `after_queue_list`; reword the `brain_digest` and `brain_stats` descriptions), `lib/brain/web/sections.py:59-95`, `lib/brain/web/worktrees.py:49`, `lib/brain/web/page.py:210-211`, `lib/brain/selftest.py` (drop the copied queue and flaky checks)

**Interfaces:**
- Produces: `forward(argv: list[str]) -> int`. It finds `test-phase` on PATH, then the newest `~/.claude/plugins/cache/*/lean-agent-tools/*/tools/test-phase`, prints `brain: "brain <cmd>" moved to "test-phase <cmd>"` to stderr and runs it with the same argv, stdin and stdout. Not found: exit 2 with that line plus `, and test-phase was not found`.

- [ ] **Step 1: Write failing checks**: a fresh brain.db is version 9 with no `jobs` or `runs` table and `PRAGMA table_info(items)` still has `run_id`; a version 9 file with jobs and runs keeps both, row for row, after `connect`; a version 4 file reaches 9 and keeps its `jobs` rows; `brain digest` and `brain resume` print no job lines; `brain stats --json` has `by_kind_status`, `open_by_project`, `stale_agents`, `median_close_s` and no `suites`; `brain queue list --status queued` with a fake `test-phase` on PATH runs it with `queue list --status queued` and prints the moved line; with none it exits 2; the MCP `tools/list` is exactly the six brain tools; the page renders with no queue or run section.
- [ ] **Step 2: Run** `python3 tools/brain --self-test` and `python3 tools/brain-mcp --self-test`. Expected: FAIL.
- [ ] **Step 3: Implement.** `migrate_2` creates `items` (with `run_id INTEGER`, no reference) and `tags`; `migrate_3`, `4`, `6`, `7`, `8` return at once; `migrate_9`, `ensure_extras` and the check at `store.py:239` skip `jobs` and `runs` when `sqlite_master` lacks them.
- [ ] **Step 4: Run** both self-tests, `python3 lib/brain/web/sections.py --self-test`, and `git grep -nE "\bjobs\b|\bruns\b|test_project|dotnet" -- lib/brain tools/brain tools/brain-mcp`. Expected: self-tests pass; the grep shows only `store.py` migrations and `forward.py`.
- [ ] **Step 5: Commit** `git commit -m "take the queue and run history out of brain"`

### Task 9: Brain's own hooks

**Files:**
- Create: `lib/brain/hooks.py`, `hooks/brain-session.py`
- Modify: `hooks/brain-resume.py`, `hooks/serve-start.py` (off `hookkit`, onto `lib/brain/hooks.py`), `hooks/session-env.py` (drop the brain parts at 37-48 and 206-215; split the order check at 253), `hooks/hooks.json` and `hooks/dispatch.json` (regenerated)

**Interfaces:**
- Produces, in `lib/brain/hooks.py`: `read_event() -> dict`; `context(event_name: str, text: str) -> None` (prints `hookSpecificOutput.additionalContext`); `append_env(lines: list[str], ours: Callable[[str], bool]) -> None` (replaces the lines `ours` matches, under an `fcntl` lock on the env file, as `session-env.py:79-131` does); `off(switch: str) -> bool` (the switch is `off`, or `LEAN_PROFILE=off`); `run(main: Callable[[], None]) -> None` (catches every exception, exits 0).
- `brain-session.py` keeps the HOOK events of `session-env.py` (SessionStart, SessionEnd, UserPromptSubmit, timeout 10) and calls `items` functions in process instead of running `brain session start|end` and `brain answers`.

- [ ] **Step 1: Write failing checks**: `session-env.py` then `brain-session.py`, and the reverse order, then both again as a resume, leave `LEAN_SESSION_ID` once, `LEAN_SESSION_CWD` once, and one PATH line for each tools folder; a shell sourcing the file finds `brain` in brain's tools folder; `LEAN_PROFILE=off` makes all three brain hooks print and write nothing; a `main` that raises exits 0 with empty output; `LEAN_SESSION_ENV=off` still writes lean-agent-tools' PATH line; `brain-resume.py` on `compact` prints the digest; `serve-start.py` still reads `CLAUDE_PLUGIN_OPTION_SERVE_MODE` and `CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS`.
- [ ] **Step 2: Run** each hook's `--self-test`. Expected: FAIL.
- [ ] **Step 3: Implement.** Both plugins' `ours` match every `LEAN_SESSION_ID=` and `LEAN_SESSION_CWD=` line and only their own PATH line, so the second writer replaces the session lines with the same values. Keep a `HOOK` literal in each brain hook so `tools/lean-agent build` still registers them in this repo, then run the build.
- [ ] **Step 4: Run** `git grep -nE "hookkit|testq|brainlink" -- lib/brain hooks/brain-*.py hooks/serve-start.py` and `bash tools/heavy.sh python bash tests/run-all.sh`. Expected: no grep output; `0 failed`.
- [ ] **Step 5: Commit** `git commit -m "give brain its own session, digest and page hooks"`, then `git branch split-base` (local only, the base for Tasks 10 and 12).

## Part B: the brain repo

### Task 10: Cut `arnelirobles/brain` locally

**Files** (in `~/repos/brain`, which must not exist yet):
- Create: `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `hooks/hooks.json` (hand-written from the three HOOK literals), `LICENSE` (MPL-2.0, from lean-agent-tools), `README.md`, `tests/run-all.sh`, `tests/contract.py`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: plugin `brain@brain`, version `1.0.0`, `mcpServers.brain` = `${CLAUDE_PLUGIN_ROOT}/tools/brain-mcp`, `userConfig.serve_mode` and `worktree_roots` as in lean-agent-tools' `plugin.json:17-30`. `tests/contract.py` holds the list of every call lean-agent-tools and the extension make (spec, "The contract") and the tables and columns the extension reads.

- [ ] **Step 1: Cut.** `git clone --no-local --single-branch --no-tags --branch split-base ~/lean-wt/brain-split ~/repos/brain`, then `git filter-repo --path lib/brain --path tools/brain --path tools/brain-mcp --path hooks/brain-resume.py --path hooks/brain-session.py --path hooks/serve-start.py --path docs/specs/2026-10-20-team-brain-design.md --path docs/specs/2026-10-24-brain-web-page-design.md --path docs/plans/2026-10-24-brain-web-page.md --path docs/specs/2026-11-21-brain-split-design.md --path docs/plans/2026-11-21-brain-split.md`, then `git branch -m split-base main`. Then `~/lean-wt/brain-split/tools/secret-scan scan --repo ~/repos/brain --range HEAD`. Expected: no findings.
- [ ] **Step 2: Write failing checks**: `tests/contract.py --self-test` runs each listed call against a scratch DB and checks the exit code and, for `--json` calls, the keys; `lib/brain/version.py` matches `plugin.json`'s version; `claude plugin validate .` exits 0 when `claude` is present.
- [ ] **Step 3: Add the files.** `tests/run-all.sh` keeps the `--self-test` glob, `LEAN_BRAIN_DB` scratch, the 300 s cap and the JSON duplicate-key check. CI jobs: `self-tests` (`bash tests/run-all.sh`), `works-alone` (fresh runner, no lean-agent-tools: `add`, `list`, `digest`, MCP `initialize` and `tools/list` lists the six tools). README: install (`claude plugin marketplace add arnelirobles/brain`, `claude plugin install brain@brain`), MCP config for Codex and Cursor pointing at `tools/brain-mcp`, commands, switches. In `docs/specs/2026-10-20-team-brain-design.md:24`, drop "queue jobs" from what sync shares and add one line that a team queue belongs to lean-agent-tools.
- [ ] **Step 4: Run** `bash tests/run-all.sh` in `~/repos/brain` with `PATH=/usr/bin:/bin` and no `~/.claude/plugins/cache/*/lean-agent-tools` on the path. Expected: `0 failed`.
- [ ] **Step 5: Commit** `git commit -m "make brain its own plugin"`. Do not create the GitHub repo or push.

## Part C: lean-agent-tools 2.0.0

### Task 11: Remove brain from lean-agent-tools

**Files:**
- Delete: `lib/brain/`, `tools/brain`, `tools/brain-mcp`, `hooks/brain-resume.py`, `hooks/brain-session.py`, `hooks/serve-start.py`, `vscode/`, the three brain docs listed in Task 10
- Modify: `.claude-plugin/plugin.json` (drop `mcpServers.brain` and `userConfig`, version `2.0.0`, description), `.claude-plugin/marketplace.json`, `hooks/hooks.json` and `hooks/dispatch.json` (regenerated), `tests/run-all.sh` (drop `lib/brain/web/*.py`), `.github/workflows/ci.yml` (drop `extension`, add `with-brain`), `skills/tools/SKILL.md`, `hooks/script-note.py`, `hooks/wait-loop.py` (`brain queue` text and self-tests), `lib/hookkit.py:1860-1881` (the `tool_path("brain")` checks use `test-phase`), `README.md`

**Interfaces:**
- Consumes: brain at tag `v1.0.0` in the `with-brain` CI job.

- [ ] **Step 1: Write failing checks**: `script-note.py` and `wait-loop.py` messages say `test-phase queue add`; `lib/testq/selftest.py` with `LEAN_E2E_BRAIN=<path to brain>` runs one drain end to end against the real brain and finds the failure with `brain list --json --kind test-fail`.
- [ ] **Step 2: Run** those self-tests. Expected: FAIL.
- [ ] **Step 3: Delete and edit.** The `with-brain` job checks out `arnelirobles/brain` at `v1.0.0` into `../brain` with no token and runs `LEAN_E2E_BRAIN=../brain/tools/brain python3 lib/testq/selftest.py --self-test`. The `self-tests` job is the works-without-brain job: its first step is `! command -v brain`, so Task 5's no-brain drain runs with no brain anywhere. README leads with "install brain for failures and questions".
- [ ] **Step 4: Run** `bash tools/heavy.sh python bash tests/run-all.sh` with `LEAN_E2E_BRAIN=~/repos/brain/tools/brain`, and `git grep -nE "from brain|import brain|brain\.db|tool_path\(\"brain\"\)" -- lib tools hooks`. Expected: `0 failed`; the grep shows only `lib/testq/store.py`.
- [ ] **Step 5: Commit** `git commit -m "remove brain and the extension from lean-agent-tools"`

## Part D: the extension

### Task 12: Cut `arnelirobles/lean-agent-vscode` locally

**Files** (in `~/repos/lean-agent-vscode`, which must not exist yet):
- Modify: `LICENSE` (MPL-2.0), `package.json` (`license`, `repository`, `version` `1.0.0`), `README.md`
- Create: `.github/workflows/ci.yml` (the old `extension` job, at the repo root)
- Delete: `test/schema.test.ts` (it reads `lib/brain`; Task 16 replaces it)

- [ ] **Step 1: Cut.** `git clone --no-local --single-branch --no-tags --branch split-base ~/lean-wt/brain-split ~/repos/lean-agent-vscode`, `git filter-repo --subdirectory-filter vscode`, `git branch -m split-base main`, then `~/lean-wt/brain-split/tools/secret-scan scan --repo ~/repos/lean-agent-vscode --range HEAD` and `git log --all --name-only --format= | grep -i '\.vsix$'`. Expected: no findings, no output.
- [ ] **Step 2: Edit** the files above.
- [ ] **Step 3: Run** `npm ci`, `npm run typecheck` and `bash ~/lean-wt/brain-split/tools/heavy.sh node npx vitest run`. Expected: all pass.
- [ ] **Step 4: Commit** `git commit -m "make the Lean Brain extension its own repo"`

### Task 13: Read `brain.db` and `tests.db`

**Files:**
- Modify: `src/brainCli.ts:8-31` (add `defaultTestsDbPath`), `package.json` (setting `leanBrain.testsDbPath`, scope `machine`), `src/store.ts` (one `Store` per file), `src/db.ts`, `src/queries.ts:51-69`, `src/extension.ts`, `src/views/itemsTree.ts`, `src/views/queueTree.ts`, `test/fixture.ts`, `test/db.test.ts`

**Interfaces:**
- Produces: `defaultTestsDbPath(setting: string, env?: NodeJS.ProcessEnv, home?: string): string` (setting, then `LEAN_TESTS_DB`, then `~/.local/share/lean/tests.db`); `interface Dbs { brain?: Database; tests?: Database }`; `queue(tests: Database)`; `cycles(dbs: Dbs): CycleRow[]`, `suites(dbs: Dbs)`, `headline(dbs: Dbs, now: number)`, `dashboard(dbs: Dbs, now: number)`; `KNOWN_BRAIN_SCHEMA = 9`, `KNOWN_TESTS_SCHEMA = 1`; `schemaNote(dbs: Dbs): string` (empty when both are known). Fixture: `BRAIN_SCHEMA`, `TESTS_SCHEMA`, `seededDbs(): Promise<Dbs>`.

- [ ] **Step 1: Write failing tests**: `defaultTestsDbPath` order; `cycles` gives `flaky_filed` 2 for a cycle with two flaky items in brain and runs in tests; with `brain` missing, `cycles` gives `flaky_filed` 0 and the items view shows `Install brain to see items.`; with `tests` missing, the queue view shows `Install lean-agent-tools to see the test queue.` and the items view still lists items; `tests.db` at schema 2 gives `tests.db is newer than this extension knows. Update Lean Brain.`
- [ ] **Step 2: Run** `bash ~/lean-wt/brain-split/tools/heavy.sh node npx vitest run test/db.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.** The `flaky_filed` subquery at `queries.ts:57` becomes `SELECT cycle, COUNT(*) AS n FROM items WHERE kind='flaky' GROUP BY cycle` on brain, joined to the runs rows in TypeScript. Both files are watched and refreshed alike.
- [ ] **Step 4: Run** typecheck and all vitest. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "read the queue and runs from tests.db"`

### Task 14: Queue actions through test-phase, and no deprecation notice

**Files:**
- Modify: `src/brainCli.ts:33-100` (`findBrain` looks in the brain plugin cache; add `findTestPhase`, `runTestPhase`, `testPhaseCanMove`), `src/extension.ts`, `package.json` (setting `leanBrain.testPhasePath`; `leanBrain.brainPath` description), `test/cli.test.ts`
- Create: `src/page.ts` (`PAGE_URL`, `openPage` from `src/deprecation.ts`), `test/page.test.ts`
- Delete: `src/deprecation.ts`, `test/deprecation.test.ts`

**Interfaces:**
- Produces: `findBrain(setting, env?, home?)` checks the setting, PATH, then the newest `~/.claude/plugins/cache/*/brain/*/tools/brain`; `findTestPhase(setting: string, env?: NodeJS.ProcessEnv, home?: string): string | undefined` checks the setting, PATH, then the newest `~/.claude/plugins/cache/*/lean-agent-tools/*/tools/test-phase`; `runTestPhase(tp: string, args: string[], testsDbPath: string, cwd?: string): Promise<CliResult>` (env `LEAN_TESTS_DB`, timeout 30 s); `testPhaseCanMove(tp: string): Promise<boolean>`.

- [ ] **Step 1: Write failing tests**: both finders pick numeric-newest (`1.10.0` over `1.2.0`); move and route run test-phase with `queue move 7 up` and `queue route 7 --to remote` and never brain; `openPage` opens `http://leanagent.localhost:8770`; activation shows no deprecation message and stores no `leanBrain.deprecationShownFor`.
- [ ] **Step 2: Run** vitest on `test/cli.test.ts` and `test/page.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** typecheck and all vitest. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "run queue actions through test-phase and drop the deprecation notice"`

### Task 15: Offer to install the plugins it needs

**Files:**
- Create: `src/setup.ts`, `test/setup.test.ts`
- Modify: `src/extension.ts` (call `checkPlugins` on activation), `package.json` (command `leanBrain.installPlugins`, "Lean Brain: Install missing plugins")

**Interfaces:**
- Consumes: `resolveClaude(setting, pathEnv, isExec, ...)` in `src/claude.ts:71`.
- Produces: `REQUIRED: { id: string; marketplace: string; repo: string }[]` = `lean-agent@lean-agent` (`arnelirobles/lean-agent`), `lean-agent-tools@lean-agent-tools` (`arnelirobles/lean-agent-tools`), `brain@brain` (`arnelirobles/brain`); `missingPlugins(pluginListJson: string): typeof REQUIRED` (an id counts as installed at any scope when `enabled` is true); `setupCommands(missing: typeof REQUIRED, marketplaceListJson: string): string[]` (a marketplace counts as listed when an entry's `name` matches); `interface SetupHost { claude?: string; run(cmd: string, args: string[], timeoutMs: number): Promise<{ code: number; stdout: string }>; ask(text: string, ...buttons: string[]): Thenable<string | undefined>; terminal(name: string): { sendText(t: string): void; show(): void }; copy(text: string): Thenable<void>; log(line: string): void }`; `checkPlugins(host: SetupHost): Promise<void>`. `extension.ts` builds the real `SetupHost` from `vscode.window`, `vscode.env.clipboard` and `execFile`.

- [ ] **Step 1: Write failing tests**: `missingPlugins` on `claude plugin list --json` output holding all three gives `[]`, without `brain@brain` gives the brain entry, with brain disabled also gives it; `setupCommands` for missing brain with no `brain` marketplace gives `claude plugin marketplace add arnelirobles/brain` then `claude plugin install brain@brain --scope user`, and with the marketplace listed gives only the install line; with no claude found, the notice's button copies those lines and opens no terminal; the terminal factory is not called until Install is clicked; Not now stores nothing, so the next activation asks again; a `claude plugin list` that fails or passes 15 s shows no notice and logs one line.
- [ ] **Step 2: Run** vitest on `test/setup.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement.** The notice reads `Lean Brain needs <names>.` with Install and Not now. Install opens a terminal named `Lean Brain setup`, sends each command with `sendText`, then `echo Restart open Claude Code sessions to load the new plugins.`
- [ ] **Step 4: Run** typecheck and all vitest. Expected: pass.
- [ ] **Step 5: Commit** `git commit -m "offer to install lean-agent, lean-agent-tools and brain"`

### Task 16: Test against real databases from pinned tags

**Files:**
- Create: `scripts/fixture-dbs.sh`, `test/real-db.test.ts`
- Modify: `.github/workflows/ci.yml` (job `real-databases`, env `BRAIN_REF: v1.0.0`, `TOOLS_REF: v2.0.0`)

**Interfaces:**
- Produces: `scripts/fixture-dbs.sh <brain-dir> <tools-dir> <out-dir>` writes `brain.db` and `tests.db` by running the real `brain add` (a test-fail and a flaky item in cycle `c1`), `test-phase queue add`, and `test-phase run-add --cycle c1 --project demo --test-project Demo.Tests`, with `LEAN_BRAIN_DB` and `LEAN_TESTS_DB` pointed into `<out-dir>`.

- [ ] **Step 1: Write the test**: skipped unless `LEAN_FIXTURE_DIR` is set; loads both files with `Store`; every query in `src/queries.ts` prepares and steps on its database without an SQL error; each column the extension reads exists (`PRAGMA table_info`), which is what `schema.test.ts` checked from source.
- [ ] **Step 2: Run** `bash scripts/fixture-dbs.sh ~/repos/brain ~/lean-wt/brain-split /tmp/fx` then `LEAN_FIXTURE_DIR=/tmp/fx` vitest on `test/real-db.test.ts`. Expected: pass (it fails first only if Task 13 missed a column).
- [ ] **Step 3: Commit** `git commit -m "test the extension against databases built by brain and test-phase"`

## Part E: release and the switch (each step on Arnel's yes)

### Task 17: Publish, release and switch

- [ ] **Step 1:** Arnel approves creating `arnelirobles/brain` and `arnelirobles/lean-agent-vscode` (public) and pushing both. Then push, and run the adversarial review on each before its first PR leaves draft.
- [ ] **Step 2:** Push `brain-split` to lean-agent-tools as a draft PR. CI's `with-brain` job waits on brain's tag.
- [ ] **Step 3:** On Arnel's yes, tag and release brain `v1.0.0`; rerun lean-agent-tools CI; merge and tag `v2.0.0`; then the extension's `real-databases` job; tag `v1.0.0` and build the `.vsix` with `npm run package`.
- [ ] **Step 4:** Arnel creates a Marketplace token and publishes `baryo-dev.lean-brain` 1.0.0.
- [ ] **Step 5:** The switch, in one sitting: `brain queue list --status queued` and `--status running` print nothing and no drainer runs; `claude plugin marketplace add arnelirobles/brain`; `claude plugin install brain@brain --scope user`; `claude plugin marketplace update lean-agent-tools`; `claude plugin update lean-agent-tools@lean-agent-tools --scope user`; remove the two local-scope 1.0.0 copies, which would otherwise keep loading the old brain server in their folders: `(cd ~ && claude plugin uninstall lean-agent-tools@lean-agent-tools --scope local)` and `(cd ~/.claude/projects/-Users-arnelirobles/memory && claude plugin uninstall lean-agent-tools@lean-agent-tools --scope local)`, and rerun `claude plugin list --json` first in case new ones appeared; `code --install-extension lean-brain-1.0.0.vsix`; restart Claude Code. Check: `claude plugin list --json` shows `lean-agent-tools@lean-agent-tools` once, at user scope, version 2.0.0, and `brain@brain` once; `test-phase queue list --status done` shows the old history, `/mcp` lists `plugin:brain:brain` once and no `plugin:lean-agent-tools:brain`, and the extension shows items and the queue.
- [ ] **Step 6:** Update `~/.claude/CLAUDE.md` "Tests and open items" (Arnel approves the wording), `skills/tools/SKILL.md` if anything is left, and the memory notes that quote `brain queue add`. Close lean-agent #143 with links to the new repo.
