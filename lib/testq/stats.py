import json

import brainlink

from .store import connect

BRAIN_TIMEOUT = 5
NO_BRAIN = "brain not installed"


def stats(con):
    out = {}
    out["suites"] = [dict(r) for r in con.execute(
        "SELECT r.project, r.test_project, COUNT(*) AS runs, ROUND(AVG(r.lane_wait_s), 1) AS mean_lane_wait_s,"
        " ROUND(AVG(r.duration_s), 1) AS mean_duration_s"
        " FROM runs r WHERE r.tier != 'rerun' GROUP BY r.project, r.test_project ORDER BY r.project, r.test_project")]
    items = brainlink.list_json(["--kind", "flaky"], timeout=BRAIN_TIMEOUT)
    where = {r["id"]: (r["project"], r["test_project"]) for r in con.execute(
        "SELECT id, project, test_project FROM runs")} if items else {}
    flaky = {}
    for it in items or []:
        try:
            suite = where.get(int(it["run_id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if suite:
            flaky[suite] = flaky.get(suite, 0) + 1
    for s in out["suites"]:
        if items is None:
            s["flaky_items"] = s["flaky_rate"] = NO_BRAIN
            continue
        s["flaky_items"] = flaky.get((s["project"], s["test_project"]), 0)
        s["flaky_rate"] = round(s["flaky_items"] / s["runs"], 3) if s["runs"] else 0
    out["slowest"] = [dict(r) for r in con.execute(
        "SELECT id, project, test_project, tier, duration_s FROM runs WHERE duration_s IS NOT NULL"
        " ORDER BY duration_s DESC, id LIMIT 5")]
    return out


def grid(heads, rows):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    w = [max(len(x[i]) for x in rows + [heads]) for i in range(len(heads))]
    return "\n".join("  ".join(c.ljust(n) for c, n in zip(r, w)).rstrip() for r in [heads] + rows)


def cmd_stats(a):
    s = stats(connect())
    if a.json:
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0
    print("suites (fast and full runs)")
    print(grid(["project", "test project", "runs", "flaky items", "flaky rate", "mean lane wait s", "mean duration s"],
               [[r["project"], r["test_project"], r["runs"], r["flaky_items"], r["flaky_rate"],
                 r["mean_lane_wait_s"], r["mean_duration_s"]] for r in s["suites"]]))
    print("\nslowest runs")
    print(grid(["run", "project", "test project", "tier", "duration s"],
               [[r["id"], r["project"], r["test_project"], r["tier"], r["duration_s"]] for r in s["slowest"]]))
    return 0
