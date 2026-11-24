"""The test queue, runs and flaky clearing, kept in tests.db. brain only holds the items they file.

  test-phase run-add --cycle C --project P --test-project T --tier fast|full|rerun [--repo R] [--runner mtp|vstest|vitest|playwright|gradle|go]
            [--started E] [--ended E] [--lane-wait N] [--load-start X] [--load-end X] [--passed N]
            [--failed N] [--skipped N] [--stalled 0|1] [--exit-code N] [--host NAME]      prints the run id
  test-phase flaky-clear --project P --test-project T [--runs 5]      closes each open flaky item of T with no new
            occurrence in that many passing full runs of T (unfiltered, exit 0, tests passed, started after the
            item was last seen), with resolution flaky-cleared. Runs carry no branch, so the item's branch is
            not looked at. --runs is at least 1 (test-phase calls it)
  test-phase queue add <repo-dir> <test-project> [--filter F] [--tier fast|full] [--after ID] [--cycle C]
            [--by NAME] [--project P] [--session ID] [--infer-session] [--platform apple|auto|none] [--again REASON] [--args=EXTRA]      prints the job id and returns at once; EXTRA is
            extra dotnet arguments for this job, written --args=-p:X=1 so a leading dash is not read as an option.
            A test project path ending in package.json is a Vitest job, and one ending in playwright.config.ts, .js, .mjs or .cjs is a Playwright job (test-phase runs either in that folder, with pnpm or npm).
            A test project path ending in settings.gradle.kts or settings.gradle, or the folder holding one, is a Gradle job
            (test-phase runs the repo's ./gradlew; the default task is check, --args='jvmTest ...' names other tasks). A Gradle job is private
            (local lane only, and --platform none is refused), and apple when its args name an iOS test task
            --platform auto (the default) marks the job apple when the test project targets -maccatalyst, -ios
            or -macos, and an npm job whose .npmrc names a private registry or holds a login private; a remote lane
            never claims an apple or private job
            The job records the repo HEAD. A full job for a project, test project and commit that already has a
            queued, running or done full job (one without a filter) is refused with that job's id, and so is a
            full job from a tree with uncommitted or untracked files (the refusal names up to 5 paths) or
            where git status fails; --again REASON allows any of these (one line, 200 characters at most).
            A folder that is not a git repo, or has no commit yet, gets neither check. An add identical to a queued job prints
            that job's id instead of adding one.
  test-phase queue list [--cycle C] [--status S]     --status queued lists in the order the drainer takes them
  test-phase queue import [--cancel-live]      run the one-time copy from brain.db now. With queued or running jobs
            in brain.db it is refused; --cancel-live copies them as cancelled (reason "queued under 1.x, not
            run") and never writes brain.db
  test-phase queue bump ID                      a queued job, and the jobs chained after it, to the front
  test-phase queue move ID up|down              swap a queued job with the one before or after it; a job never
            moves ahead of the job it waits on
  test-phase queue cancel ID                    a queued job; jobs waiting on it are cancelled too
  test-phase queue route ID --to remote|local|any [--force]   remote: only a remote lane claims it; local: only this
            machine (platform=apple); any: whichever lane is free. remote refuses an Apple or private job, and any
            refuses a private job, without --force; a Gradle job can only be routed local
  test-phase queue wait ID... [--timeout SECS] [--interval SECS]   blocks until every named job ends, then prints one
            line per job (id, status, passed and failed of its run, the item ids it filed). Exit 0 when all are
            done, 1 when any failed or was cancelled, 2 for an unknown id, 3 on timeout (default 7200 s; the
            pending jobs and their state are printed). It polls the database every 15 s itself. Start it with
            run_in_background and end the turn.
  test-phase queue next [--peek] [--where local|remote] [--host NAME]   (what test-phase uses to drain)
  test-phase queue finish ID --status done|failed | requeue ID --reason R   (requeue puts a running job back)
  test-phase stats [--json]      suites (runs, mean lane wait and duration, flaky items) and the slowest runs;
            flaky items come from brain and read "brain not installed" without it

The file is ${LEAN_TESTS_DB:-~/.local/share/lean/tests.db}. The first open copies the jobs and runs of an old
brain.db and refuses while that file still has queued or running jobs. A plan file named queue is passed as ./queue.

The queue replaces wait loops. Never write a sleep or pgrep loop to wait for the test lane: enqueue and
end the turn, or start `test-phase queue wait ID...` in the background for one wake-up when the jobs end. tools/test-phase drains the queue, one drainer at a time, oldest ready job first. A job
whose --after job failed or was cancelled is cancelled with the reason. States: queued, running, done,
failed, cancelled.
"""
import argparse
import os
import sys

from .flaky import cmd_flaky_clear
from .queue import JOB_STATUS, JOB_TIERS, cmd_queue
from .runs import RUNNERS, TIERS, cmd_run_add
from .stats import cmd_stats
from .store import CopyRefused, NewerSchema


def lower(s):
    name = (s or "").strip().lower()
    if not name:
        raise argparse.ArgumentTypeError("a project name cannot be empty")
    return name


def seconds(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number")
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1 second")
    return value


def parser():
    p = argparse.ArgumentParser(prog="test-phase", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run-add")
    s.add_argument("--cycle", default="")
    s.add_argument("--project", required=True, type=lower)
    s.add_argument("--repo", default="")
    s.add_argument("--test-project", required=True)
    s.add_argument("--runner", default="", choices=RUNNERS)
    s.add_argument("--tier", required=True, choices=TIERS)
    s.add_argument("--started", type=int)
    s.add_argument("--ended", type=int)
    s.add_argument("--lane-wait", type=float)
    s.add_argument("--load-start", type=float)
    s.add_argument("--load-end", type=float)
    s.add_argument("--passed", type=int, default=0)
    s.add_argument("--failed", type=int, default=0)
    s.add_argument("--skipped", type=int, default=0)
    s.add_argument("--stalled", type=int, default=0, choices=(0, 1))
    s.add_argument("--exit-code", type=int)
    s.add_argument("--host", default="")
    s.set_defaults(fn=cmd_run_add)
    s = sub.add_parser("flaky-clear")
    s.add_argument("--project", required=True)
    s.add_argument("--test-project", required=True)
    s.add_argument("--runs", type=int, default=5)
    s.set_defaults(fn=cmd_flaky_clear)
    s = sub.add_parser("queue")
    qs = s.add_subparsers(dest="qcmd", required=True)
    q = qs.add_parser("add")
    q.add_argument("repo_dir")
    q.add_argument("test_project")
    q.add_argument("--filter", default="")
    q.add_argument("--tier", choices=JOB_TIERS, default="full")
    q.add_argument("--after", type=int)
    q.add_argument("--cycle", default="")
    q.add_argument("--by", default="")
    q.add_argument("--project", default=None, type=lower)
    q.add_argument("--args", default="")
    q.add_argument("--platform", choices=("apple", "auto", "none"), default="auto")
    q.add_argument("--session", default=os.environ.get("LEAN_SESSION_ID", ""),
                   help="the session that queued it (default $LEAN_SESSION_ID); its items carry it")
    q.add_argument("--session-cwd", default=os.environ.get("LEAN_SESSION_CWD", ""))
    q.add_argument("--infer-session", action="store_true",
                   help="with no session, take the open one brain finds for this folder")
    q.add_argument("--again", default="", metavar="REASON")
    q = qs.add_parser("list")
    q.add_argument("--cycle")
    q.add_argument("--status", choices=JOB_STATUS)
    q = qs.add_parser("import")
    q.add_argument("--cancel-live", action="store_true",
                   help="copy queued and running jobs of brain.db as cancelled instead of refusing")
    q = qs.add_parser("wait")
    q.add_argument("ids", type=int, nargs="+", metavar="ID")
    q.add_argument("--timeout", type=seconds, default=7200, help="seconds before giving up (exit 3), at least 1")
    q.add_argument("--interval", type=seconds, default=15, help="seconds between database polls, at least 1")
    q = qs.add_parser("cancel")
    q.add_argument("id", type=int)
    q = qs.add_parser("bump")
    q.add_argument("id", type=int)
    q = qs.add_parser("move")
    q.add_argument("id", type=int)
    q.add_argument("where", choices=("up", "down"))
    q = qs.add_parser("next")
    q.add_argument("--peek", action="store_true")
    q.add_argument("--where", choices=("local", "remote"), default="local")
    q.add_argument("--host", default="")
    q = qs.add_parser("route")
    q.add_argument("id", type=int)
    q.add_argument("--to", choices=("remote", "local", "any"), required=True)
    q.add_argument("--force", action="store_true", help="send an Apple job to a remote lane anyway")
    q = qs.add_parser("requeue")
    q.add_argument("id", type=int)
    q.add_argument("--reason", required=True)
    q = qs.add_parser("finish")
    q.add_argument("id", type=int)
    q.add_argument("--status", choices=("done", "failed"), required=True)
    q.add_argument("--run-id", type=int)
    q.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_queue)
    s = sub.add_parser("stats")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)
    return p


def main(argv):
    args = parser().parse_args(argv)
    try:
        return args.fn(args)
    except CopyRefused as e:
        print(f"test-phase: brain.db still has {e.count} queued or running jobs. "
              "Drain them with lean-agent-tools 1.x, or run `test-phase queue import --cancel-live` "
              "to copy them as cancelled.", file=sys.stderr)
        return 2
    except NewerSchema as e:
        print(f"test-phase: {e}. Update lean-agent-tools.", file=sys.stderr)
        return 2
