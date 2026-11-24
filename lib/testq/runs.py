import time

from .store import connect

TIERS = ("fast", "full", "rerun")

RUNNERS = ("mtp", "vstest", "vitest", "playwright", "gradle", "go", "")


def cmd_run_add(a):
    con = connect()
    started = a.started if a.started is not None else int(time.time())
    ended = a.ended if a.ended is not None else started
    with con:
        cur = con.execute(
            "INSERT INTO runs (cycle, project, repo, test_project, runner, tier, started, ended, duration_s,"
            " lane_wait_s, load_start, load_end, passed, failed, skipped, stalled, exit_code, host)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a.cycle, a.project, a.repo, a.test_project, a.runner, a.tier, started, ended, ended - started,
             a.lane_wait, a.load_start, a.load_end, a.passed, a.failed, a.skipped, a.stalled, a.exit_code, a.host))
    print(cur.lastrowid)
    return 0
