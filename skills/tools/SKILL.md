---
name: tools
description: Maps each plain step in the lean-agent skills (method, adversarial-review, shape-ticket, lean-retro) to the hook, script or brain command that does it. Load it alongside the lean-agent skills whenever the lean-agent-tools plugin is installed.
user-invocable: false
---

# Lean agent tools

The lean-agent skills say what to do with git, gh, files in the repo and the repository's own test command. This plugin does several of those steps with scripts, hooks and the brain database. When a step below applies, use the tool instead of the plain version. Without lean-agent-tools, the skills work as written.

Scripts live in `${CLAUDE_PLUGIN_ROOT}/tools/`. They are on PATH once the session-env hook ran, so run one by name.

## What replaces what

| Step in the lean-agent skills | Here |
|---|---|
| State that changes lives in `docs/agent-state.md` (open agents, deferred work, questions, test failures, security findings) | `brain`: `brain add --kind agent\|pending\|minor\|blocker\|question`, `brain list --open`, `brain close`, `brain resume`, `brain digest`. The brain MCP server exposes the same. Never store secrets in it. |
| Run the repository's test command and read the output | `brain queue add <repo-dir> <test project>`, then one `test-phase` run by the orchestrator. `brain queue wait ID...` to wait. Failures land in brain. |
| Serialise heavy commands and never write a sleep loop | `heavy.sh <lane> <command>`, `heavy.sh status <lane>`, `bounded`. The `wait-loop` and `bg-bound` hooks deny the loops. |
| Scan added lines and body text for dashes, arrows, slop and attribution | `style-scan`, and the `style-note` and `public-text` hooks. |
| The mutation check: hold the production hunk out and the named test must fail | `holdout.sh --spec <file>`. |
| Check which review rules a diff fires | `needs-review.sh`. |
| Log each review in `docs/review-log.md` | `log-review.sh <repo> <pr> ...`, which writes `review-log.tsv` and the lean-stats database. The `review-log-check` hook refuses `gh pr ready` and `gh pr merge` until the pull request has a row. |
| Retro facts from git and gh | `retro-signals.sh --since <date> owner/repo ...`, `lean-stats collect` and `lean-stats report` (cost per change by model and role, escalation rate, critic misses, avoidable tokens), `script-repeats --days 7`, `workflow-cost.py`. |
| Label every agent by role and change | Same label, counted by `lean-stats`. |
| API-credit runs for the hunter, refuter, spec review and drafting | `credit-run` and `credit-batch`, off unless `LEAN_CREDIT_RUNS=on` and `LEAN_CREDIT_BATCH=on`. |
| Do not commit keys | `secret-scan install`, the `secret-scan` hook, and `LEAN_PROFILE`. |
| Find where a pull request is blocked, why a run is red, whether a deploy is live | `pr-blockers.sh`, `pr-wait`, `why-red.sh`, `assert-ran.sh`, `check-deployed-sha.sh`. |
| Orient in a repository and its worktrees, clean up after parallel agents | `orient [--all]`, `agent-hygiene.sh`, `sync-default.sh`. |
| Cheap repository checks in the preflight | `check-conflict-markers.sh`, `check-top-level-md.sh`, `check-workflow-keys.py`, `check-runtime.sh`, `check-pinned-values.sh`. |
| Exact text edit, latest package version | `replace`, `pkg-version`. |

## Mode
Two settings change how the lean-agent rules apply: `LEAN_AGENTS` (`many` or `one`) and `LEAN_MODELS` (`mixed` or `one`), in the `env` block of Claude Code settings. At session start the plugin notes any setting that is not the default and what it changes. No note means many agents and mixed models, as the lean-agent skills write it.

With mixed models, three more settings name the models: `LEAN_MODEL_CHEAP` (default `sonnet`), `LEAN_MODEL_STRONG` (default `opus`) and `LEAN_MODEL_FAST` (default `haiku`), each a Claude Code model alias. Start the drafter, the spec review and the critic's hunter and refuter with the cheap model. The strong model takes the branch and the critic's list only when a finding is not fixed in one round or a tier 2 answer is unsure. The fast model runs agents that read and report: `search` (locate the code a draft or review needs), `gate` (run the scripted gates and paste their output) and `retro` (the retro's fact collection). It never judges code, so the hunter and the refuter do not run on it. No note means `sonnet`, `opus` and `haiku`.

With `LEAN_CREDIT_RUNS=on`, the spec review, the critic's hunter and refuter, analysis and written documents run on API credits through `credit-run --read-only --role spec|hunt|refute|analyze|draft-doc`. Coding, orchestration, `search`, `gate` and `retro` stay on the session's plan. With `LEAN_CREDIT_BATCH=on` too, single-shot work that can wait (retro digests, ticket shaping, spec review) can go through `credit-batch`. The critic stays live. With `LEAN_CREDIT_CODE=on` too, `lean:draft` work runs as `credit-run` edit jobs in a clean linked worktree; the job builds only and tests stay in the queue. When `LEAN_CREDIT_UNTIL` is set, the coordinator runs `credit-run --pace` at the start of a batch and follows its last line. With `LEAN_CREDIT_ASK=on`, a case the routing does not settle (a large job, code work when behind pace, a job near the total) gets one question to the person with the cost estimate; unattended, it goes to brain as a question and the job stays on the plan.

Label every agent you start, so lean-stats can count it by role. The Agent description is `lean:<role> <owner/repo>#<pr>`, or `lean:<role> <owner/repo>@<branch>` before the pull request exists. The roles are `draft`, `spec`, `hunt`, `refute`, `escalate` (the strong model taking what the cheap one did not close), `search`, `gate` and `retro` (the last three run on the fast model). In a workflow script the same label is the first line of the agent's prompt, because a workflow agent has no description. An agent with no label is counted as `other` and its cost cannot be tied to a change.

## Scripts
The scripts live in `${CLAUDE_PLUGIN_ROOT}/tools/`. They are on PATH when the session-env hook ran, so run one by name; otherwise run it as `${CLAUDE_PLUGIN_ROOT}/tools/<name>`. The names below stand for either. A session start note also prints the absolute folder. Use them instead of writing your own.
- `style-scan`: checks the lines you add (and untracked files, so a body file counts) for dashes, arrows, slop and attribution. Run it before every commit, and on a body file before `gh ... --body-file`.
- `holdout.sh --spec <file>`: holds a change's production hunk out and requires its named test to fail. Use it for the mutation check, not a hand revert.
- `secret-scan install [REPO]`: git pre-commit and pre-push hooks that block keys and tokens in the lines a commit or push adds. The `secret-scan` Claude hook blocks a commit or push in a repo without those hooks, or one that skips them, and scans file writes. Mark a test fixture line `allow-secret`.
- `replace`: exact text edit that fails unless the old text is there exactly once.
- `wait-loop` hook (`hooks/wait-loop.py`): denies `sleep` and `pgrep` wait loops. Enqueue and end the turn instead, or start `brain queue wait ID...` or `pr-wait` in the background.
- `review-log-check` hook (`hooks/review-log-check.py`): denies `gh pr ready`, `gh pr merge` and `az repos pr update --draft false` (or `--status completed`) when `lean.db` has no review row for that pull request, once the repo has a logged review. Log it with `${CLAUDE_PLUGIN_ROOT}/tools/log-review.sh <repo> <pr> ...` first, or set `LEAN_SKIP_REVIEW_LOG_CHECK=1` for a change that was not reviewed.
- `pr-wait <pr>`: waits for a pull request's checks (or `--provider azure` for a pipeline run) and prints only the failed checks and the tail of their logs.
- `orient [--all]`: repo, branch, dirty count, ahead and behind, open PR with its checks, and with `--all` one line per worktree. Use it instead of a status loop over worktrees.
- `pkg-version <name> [--nuget|--npm]`: the latest stable version and what each project in the repo pins. Use it instead of a curl to a registry.
- `script-note` hook (`hooks/script-note.py`): when a command matches a family in `lib/script_families.json` that a script covers, or calls a script by its plugin cache path, it adds a one line note naming the script. Once per family per session. `LEAN_SCRIPT_NOTE=off` turns it off. The same hook blocks (exit 2) a direct `dotnet test` (a .sln or .csproj in reach) or `npm test`, `npx vitest`, `vitest run` (a pnpm-lock.yaml in reach) and names the `brain queue add` line to use; `LEAN_DIRECT_TEST=on` turns that off.
- `bounded [--minutes N] -- cmd`: runs a command in its own process group and stops the whole group after `LEAN_BG_MINUTES` (default 60), when the task is stopped, or when its shell dies. A SIGKILL of `bounded` itself cannot be caught and leaves the group running; `agent-hygiene.sh` reports such orphans. The `bg-bound` hook (`hooks/bg-bound.py`) denies a background command that does not run under it, `test-phase` or `heavy.sh`, and prints the wrapped form to run instead.
- `credit-run --brief FILE --worktree DIR --budget USD [--read-only --role NAME]`: runs a brief as a headless Agent SDK job on API credits, with a total cap in `~/.lean/credit-runs.jsonl`. Off unless `LEAN_CREDIT_RUNS=on`. Use `--read-only` for review and analysis jobs and take the result text from stdout. See the README.
- `credit-batch submit --brief FILE [--shared FILE] --max-tokens N`: sends read-only briefs as one Message Batch at half price, on the same ledger and total as credit-run. Off unless `LEAN_CREDIT_BATCH=on` too. `status ID` and `results ID --dir DIR` collect it. See the README.
- `pr-blockers.sh <pr>`: every reason a pull request cannot merge, in one read-only call.
- `why-red.sh <log>`: names environmental causes (no Docker, a busy port, a full disk) in a failed log before you debug code.
- `assert-ran.sh <report>`: fails when a test report shows zero tests ran.
- `check-deployed-sha.sh <url> <sha>`: proves a deploy by the commit sha it reports, not a 200.
- `brain` and `test-phase`: the issue list and the test queue (below).
- `heavy.sh <lane> <command>`: runs a heavy command under the shared lane lock; `heavy.sh status <lane>` says whether it is free.
- `sync-default.sh`: merges the default branch and runs a locked restore. Refuses a dirty tree.
- `needs-review.sh`: prints the review rules a diff fires.
- `agent-hygiene.sh`: reports what parallel agents left behind.
- `check-conflict-markers.sh`, `check-top-level-md.sh`, `check-workflow-keys.py`, `check-runtime.sh`: cheap repository checks, run them in the preflight.
- `script-repeats [--days N]`: counts the commands agents wrote by hand in recent transcripts, per family, with the script that covers each. The retro runs it.
- `retro-signals.sh`, `lean-stats`, `workflow-cost.py`: the retro's facts and what a run cost.
- `log-review.sh <repo> <pr> <confirmed> <refuted> <blockers> "<categories>" "<caught elsewhere first>" [pre_push] [ci] [review] [escaped]`: appends one review row to `review-log.tsv` and to the lean-stats database. Logging the same pull request again replaces its row there, which is how `escaped` gets filled in later.
- `lean-agent build [--check]`: builds hooks/hooks.json from the HOOK literals in hooks/*.py; `--check` says whether it is current.

### Drafter brief (`lean:draft`)
Coding agents call these instead of writing the command again:
- `replace FILE < blocks`: exact text edit that fails unless the old text is there once. Use it instead of inline python that calls `.replace`.
- `brain queue add ...`: queue the tests, end the turn, and the orchestrator runs `test-phase`. Never run `dotnet test`, `npx vitest`, `vitest run` or `npm test` yourself; the script-note hook blocks it where test-phase can take the job.
- `check-deployed-sha.sh <url> <sha>`: proves a deploy by the commit sha it reports. Use it instead of a curl smoke check.
- `style-scan`: checks added lines for dashes, arrows, slop and attribution. Use it instead of a grep, before every commit.
- `holdout.sh --spec <file>`: holds each production hunk out and requires its named test to fail. The draft is not done until it passes; run it before handing the branch to the hunt, so the hunter does not spend its pass on tests that pass for the wrong reason. On a repository whose default branch is not `master`, set `HOLDOUT_BASE=origin/<default>`.
- `pkg-version <name> [--nuget|--npm]`: the latest stable version and what the repo pins. Use it instead of a curl to a registry.

## Tests and brain
- Code first, build only. Run the tests once, as one phase: add each test project with `${CLAUDE_PLUGIN_ROOT}/tools/brain queue add <repo-dir> <test project> [--after ID]`, then the orchestrator starts one `${CLAUDE_PLUGIN_ROOT}/tools/test-phase` as a background task. Queue one job per worktree: put every class you need in one `--filter 'ATests|BTests'` rather than one job per class, run `${CLAUDE_PLUGIN_ROOT}/tools/brain queue list` first, and when an earlier job of yours for the same worktree is still queued, cancel it and add the merged one. Adding a job that is already queued returns its id, so wait on that. Failures land in `${CLAUDE_PLUGIN_ROOT}/tools/brain`. The fix round reads `${CLAUDE_PLUGIN_ROOT}/tools/brain list --open`, closes entries with a resolution and the fix commit, and the phase runs again.
- Test only what is needed while working. A fix or review round queues only the classes the change touches and the classes that failed. The full suite runs once, as the final check before pushing: the queue refuses a second full job for the same project, test project and commit, and a full job from a tree with uncommitted changes, unless you pass `--again "reason"`. Its failures are rerun one class at a time. When several branches need that final run, merge them into one candidate and run it once. A red run, the test proving it fails before the fix, stays on its own branch.
- To wait, start the wait command in the background and end the turn: `${CLAUDE_PLUGIN_ROOT}/tools/brain queue wait ID...` for test jobs, `${CLAUDE_PLUGIN_ROOT}/tools/pr-wait` for a pull request's checks or a pipeline run. Never call `${CLAUDE_PLUGIN_ROOT}/tools/brain queue list`, `gh pr checks` or `az pipelines runs show` twice in a row for the same thing.
- Never write a `sleep` or `pgrep` wait loop. Enqueue the job, end the turn, and the orchestrator's one drainer runs it. The orchestrator gets one notice when the drainer exits, then reads `${CLAUDE_PLUGIN_ROOT}/tools/brain list --cycle C` or `${CLAUDE_PLUGIN_ROOT}/tools/brain stats`. `${CLAUDE_PLUGIN_ROOT}/tools/brain resume` shows queued and running jobs after a compaction, and the startup hook prints `${CLAUDE_PLUGIN_ROOT}/tools/brain digest` for the project.
- Ask whether a lane is free with `${CLAUDE_PLUGIN_ROOT}/tools/heavy.sh status <lane>`. Never write a `pgrep` loop for it: it matches its own command line and never ends. Set `HEAVY_STALL` on test commands so a stalled run stops as load, not as a code failure.
- Check the runner mode before `dotnet test`. A repository whose `global.json` sets `test.runner` to `Microsoft.Testing.Platform` takes `dotnet test --project X`; any other takes `dotnet test X`. The wrong form loses the run.
- Brain is where state that changes lives, so it survives a compaction. Open an item when you dispatch an agent (kind `agent`: id, task, worktree), when you defer work (`pending` for the next step, `minor` for a deferred fix) or when an action is denied or waits on the person (`blocker`). Close it when the agent reports back, the work is done or the answer comes, with a resolution (or `--wontfix`). Kinds are a fixed list and an unknown one is refused. A denied action is not worked around.
- What goes where: brain holds anything with a status that changes (test failures, flaky classes, deferred minors, questions for the user, tickets, security findings). Markdown ledgers keep rulings with the cost if wrong, task complete lines and user decisions, and point to open items with `open items: brain list --project X --status open`. Memory files keep standing preferences. Run logs are scratch.
- A question for the person: file it with `${CLAUDE_PLUGIN_ROOT}/tools/brain add --kind question` (title is the question, body has the options and your recommendation), then end your turn. Do not wait in the session. Brain stores the session id, and the answer comes back to this session: the extension resumes it, or the next prompt carries it.
- Never store secrets, tokens or passwords in brain.

## After a batch
- Log every review with `${CLAUDE_PLUGIN_ROOT}/tools/log-review.sh`.
- Run the `lean-retro` skill with the facts from `retro-signals.sh` and `lean-stats`.
- Sweep with `${CLAUDE_PLUGIN_ROOT}/tools/agent-hygiene.sh` for what parallel agents left behind.
- The plugin hooks block agent attribution and slop in commits, pull requests, issues and releases. Rewrite the text; do not turn the hook off.
