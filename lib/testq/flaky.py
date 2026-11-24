import calendar
import sys
import time

import brainlink

from .store import connect

BRAIN_TIMEOUT = 5


def epoch(ts):
    """Seconds for an ISO timestamp as brain writes it, or for a number."""
    if isinstance(ts, (int, float)):
        return ts
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def cmd_flaky_clear(a):
    """Close each open flaky item of a test project that has had no new occurrence in --runs passing full runs.

    The items live in brain, the runs in tests.db. A passing run is an unfiltered full run of the test project
    that exited 0 and passed tests, started after the item was last seen. Runs carry no branch, so this does
    not look at the item's branch. Without brain there is nothing to close."""
    if a.runs < 1:
        sys.exit("test-phase: --runs must be at least 1")
    con = connect()
    items = brainlink.list_json(["--kind", "flaky", "--project", a.project, "--open"], timeout=BRAIN_TIMEOUT)
    for it in sorted(items or [], key=lambda i: i.get("id") or 0):
        try:
            run_id, seen = int(it["run_id"]), epoch(it["last_seen"])
        except (KeyError, TypeError, ValueError):
            continue
        run = con.execute("SELECT test_project FROM runs WHERE id=? AND project=?", (run_id, a.project)).fetchone()
        if not run or run["test_project"] != a.test_project:
            continue
        n = con.execute("SELECT COUNT(*) FROM runs WHERE project=? AND test_project=? AND tier='full'"
                        " AND exit_code=0 AND failed=0 AND passed>0 AND started>=?",
                        (a.project, a.test_project, seen)).fetchone()[0]
        if n < a.runs:
            continue
        out = brainlink.call(["close", str(it["id"]), "--resolution", "flaky-cleared", "--note",
                              f"no new occurrence in {n} passing runs"], timeout=BRAIN_TIMEOUT)
        if out is not None:
            print(f"closed: #{it['id']} {it.get('title') or ''}".rstrip())
    return 0
