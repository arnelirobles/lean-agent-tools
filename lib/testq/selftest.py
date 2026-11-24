import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brainlink  # noqa: E402
from testq import cli, fakebrain, queue as tq, store  # noqa: E402

# Literal DDL copied from brain's store.py (migrations 2, 3, 4, 6, 7, 8, 9 and the branch column of
# ensure_extras). The self-test never imports brain's code.
RUNS_V4 = """CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '', test_project TEXT NOT NULL DEFAULT '', runner TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'full', started INTEGER, ended INTEGER, duration_s REAL,
    lane_wait_s REAL, load_start REAL, load_end REAL, passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
    stalled INTEGER NOT NULL DEFAULT 0, exit_code INTEGER)"""
JOBS_V4 = """CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
    repo_dir TEXT NOT NULL, test_project TEXT NOT NULL, filter TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'full', after_id INTEGER REFERENCES jobs(id),
    status TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
    enqueued_by TEXT NOT NULL DEFAULT '', enqueued_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
    run_id INTEGER REFERENCES runs(id))"""
ALTER_V4 = ["ALTER TABLE jobs ADD COLUMN args TEXT NOT NULL DEFAULT ''"]
ALTER_V9 = [
    "ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN platform TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN host TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN host TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN commit_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN session_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN session_cwd TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE jobs ADD COLUMN branch TEXT NOT NULL DEFAULT ''",
]


def make_brain(path, version, jobs, runs):
    """A brain.db holding only jobs, runs and meta. jobs is a list of (id, status), runs a list of ids."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute(RUNS_V4)
    con.execute(JOBS_V4)
    for sql in ALTER_V4 + (ALTER_V9 if version == 9 else []):
        con.execute(sql)
    con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(version),))
    for rid in runs:
        con.execute("INSERT INTO runs (id, project, test_project, passed) VALUES (?, 'p', 't', 5)", (rid,))
    for i, (jid, status) in enumerate(jobs):
        con.execute("INSERT INTO jobs (id, project, repo_dir, test_project, status, enqueued_at, run_id)"
                    " VALUES (?, 'p', '/r', 't', ?, '2026-01-01T00:00:00Z', ?)",
                    (jid, status, runs[i] if i < len(runs) else None))
    con.commit()
    con.close()


def columns(path, table):
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


REAL_GIT = shutil.which("git")


@contextlib.contextmanager
def isolated(d, name, fake=None, **extra):
    """PATH holds only git and, when fake is a dict of FAKE_BRAIN_* knobs, a fake brain; HOME is empty.
    The environment and the brainlink cache are restored on exit."""
    saved = dict(os.environ)
    bin_dir, home = os.path.join(d, name + "-bin"), os.path.join(d, name + "-home")
    os.makedirs(bin_dir, exist_ok=True)
    os.makedirs(home, exist_ok=True)
    if REAL_GIT and not os.path.exists(os.path.join(bin_dir, "git")):
        os.symlink(REAL_GIT, os.path.join(bin_dir, "git"))  # queue add runs git
    if fake is not None:
        fakebrain.install_fake(bin_dir, fake)
    os.environ.update(PATH=bin_dir, HOME=home, **extra)
    for k in ("LEAN_SESSION_ID", "LEAN_SESSION_CWD"):
        os.environ.pop(k, None)
    brainlink.reset()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
        brainlink.reset()


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(list(argv))
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
    return rc, out.getvalue().strip(), err.getvalue().strip()


def fake_state(path, items):
    with open(path, "w") as f:
        json.dump({"next_id": 100, "items": items}, f)


def logged(path):
    try:
        with open(path) as f:
            return [a for a in (json.loads(ln) for ln in f) if a != ["--version"]]
    except OSError:
        return []


def queue_checks(fails, d):
    os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "none.db")
    os.environ["LEAN_TESTS_DB"] = os.path.join(d, "queue.db")
    repo = os.path.join(d, "r")

    def nxt(*extra):
        out = run("queue", "next", *extra)[1]
        return json.loads(out) if out else None

    with isolated(d, "nobrain"):
        j1 = run("queue", "add", repo, "A.csproj", "--cycle", "q", "--by", "me")[1]
        j2 = run("queue", "add", repo, "A.csproj", "--tier", "fast", "--filter", "Unit", "--after", j1)[1]
        j3 = run("queue", "add", repo, "B.csproj", "--after", j2)[1]
        j4 = run("queue", "add", repo, "C.csproj")[1]
        if not all(x.isdigit() for x in (j1, j2, j3, j4)):
            fails.append(f"queue add must print the job id: {[j1, j2, j3, j4]}")
        if run("queue", "add", repo, "A.csproj", "--tier", "fast")[0] == 0:
            fails.append("a fast job without a filter accepted")
        if run("queue", "add", repo, "A.csproj", "--after", "9999")[0] == 0:
            fails.append("a job after a missing job accepted")
        first = nxt()
        if not first or str(first["id"]) != j1 or first["status"] != "queued":
            fails.append(f"oldest ready job first: {first}")
        second = nxt()
        if not second or str(second["id"]) != j4:
            fails.append(f"a job behind an unfinished dependency must wait: {second}")
        if nxt("--peek") is not None:
            fails.append("nothing is ready while the dependencies run")
        run("queue", "finish", j4, "--status", "done")
        run("queue", "finish", j1, "--status", "done")
        third = nxt()
        if not third or str(third["id"]) != j2:
            fails.append(f"dependent not ready after its dependency finished: {third}")
        run("queue", "finish", j2, "--status", "failed", "--reason", "tests failed")
        out = run("queue", "list", "--status", "cancelled")[1]
        if f"job #{j3} cancelled" not in out or f"dependency #{j2} failed" not in out:
            fails.append(f"a failed dependency must cancel its dependents: {out!r}")
        k1 = run("queue", "add", repo, "D.csproj")[1]
        k2 = run("queue", "add", repo, "D.csproj", "--after", k1)[1]
        run("queue", "cancel", k1)
        if f"job #{k2} cancelled" not in run("queue", "list", "--status", "cancelled")[1]:
            fails.append("cancel must reach every dependent")
        rc, _, err = run("queue", "cancel", j1)
        if rc == 0 or not err.startswith("test-phase: "):
            fails.append(f"a finished job cannot be cancelled, and the message names test-phase: {rc} {err!r}")
        d1 = run("queue", "add", repo, "H.csproj", "--tier", "fast", "--filter", "X")[1]
        rc, d2, err = run("queue", "add", repo, "H.csproj", "--tier", "fast", "--filter", "X", "--by", "other")
        if rc != 0 or d2 != d1 or f"#{d1}" not in err:
            fails.append(f"an identical queued job must be joined: {d1} {d2} {err!r}")
        run("queue", "cancel", d1)

        # one full run per commit, and no full run from a dirty tree
        g = os.path.join(d, "gitrepo")
        os.makedirs(g)

        def git(*args):
            subprocess.run([REAL_GIT, "-C", g, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                           capture_output=True)
        git("init", "-q")
        with open(os.path.join(g, "f.txt"), "w") as f:
            f.write("one\n")
        git("add", "f.txt")
        git("commit", "-q", "-m", "one")
        c1 = run("queue", "add", g, "T.csproj")[1]
        run("queue", "finish", c1, "--status", "done")
        rc, _, err = run("queue", "add", g, "T.csproj")
        if rc == 0 or f"#{c1}" not in err or "done" not in err:
            fails.append(f"a second full job for a commit must be refused with the first id: {rc} {err!r}")
        rc, c2, _ = run("queue", "add", g, "T.csproj", "--again", "flaky infra")
        if rc != 0 or c2 == c1 or "again: flaky infra" not in run("queue", "list", "--status", "queued")[1]:
            fails.append(f"--again must allow a second full job: {rc} {c2!r}")
        with open(os.path.join(g, "f.txt"), "w") as f:
            f.write("edited\n")
        rc, _, err = run("queue", "add", g, "W.csproj")
        if rc == 0 or "uncommitted" not in err or "f.txt" not in err:
            fails.append(f"a dirty tree must refuse a full job and name the file: {rc} {err!r}")
        if run("queue", "add", g, "W.csproj", "--tier", "fast", "--filter", "X")[0] != 0:
            fails.append("a fast job from a dirty tree must be allowed")

        # bump, move, route, requeue
        m1, m2, m3 = (run("queue", "add", repo, f"M{i}.csproj")[1] for i in (1, 2, 3))
        mine = {m1, m2, m3}

        def order():
            lines = run("queue", "list", "--status", "queued")[1].splitlines()
            return [i for i in (ln.split()[1][1:] for ln in lines) if i in mine]
        run("queue", "move", m3, "up")
        if order() != [m1, m3, m2]:
            fails.append(f"move up must swap with the job before it: {order()}")
        run("queue", "bump", m2)
        if order() != [m2, m1, m3]:
            fails.append(f"bump must reach the front: {order()}")
        pa = run("queue", "add", repo, "Z.csproj", "--platform", "apple")[1]
        rc, out, err = run("queue", "route", pa, "--to", "remote")
        if rc == 0 or "--force" not in err:
            fails.append(f"routing an apple job to a remote lane needs --force: {rc} {err!r}")
        rc, out, _ = run("queue", "route", pa, "--to", "any")
        if rc != 0 or "platform=" in out:
            fails.append(f"route any must clear the platform: {rc} {out!r}")
        for m in mine | {pa}:
            run("queue", "cancel", m)
        for ln in run("queue", "list", "--status", "queued")[1].splitlines():
            run("queue", "cancel", ln.split()[1][1:])
        r1 = run("queue", "add", repo, "R.csproj")[1]
        got = nxt("--host", "dev")
        rc, _, _ = run("queue", "requeue", r1, "--reason", "ssh failed")
        ql = run("queue", "list", "--status", "queued")[1]
        if not got or str(got["id"]) != r1 or rc != 0 or "ssh failed" not in ql or "on=" in ql:
            fails.append(f"requeue must put a running job back with its reason: {got} {rc} {ql!r}")
        run("queue", "cancel", r1)

        # wait: unknown id exits 2, a timeout exits 3, ended jobs print one line each
        if run("queue", "wait", "9999")[0] != 2:
            fails.append("waiting on an unknown job must exit 2")
        w1 = run("queue", "add", repo, "WA.csproj")[1]
        rc, out, _ = run("queue", "wait", w1, "--timeout", "1", "--interval", "1")
        if rc != 3 or f"job #{w1}" not in out:
            fails.append(f"a wait on a queued job must time out with exit 3: {rc} {out!r}")
        run("queue", "cancel", w1)
        rc, out, _ = run("queue", "wait", w1)
        if rc != 1 or f"job #{w1} cancelled" not in out or "items=-" not in out:
            fails.append(f"a wait on a cancelled job must exit 1, no brain, items=-: {rc} {out!r}")

    # wait names the items brain holds for the job's run
    state, log = os.path.join(d, "wait-state.json"), os.path.join(d, "wait-log")
    fake_state(state, [{"id": 41, "kind": "flaky", "status": "open", "run_id": "1", "title": "t"},
                       {"id": 42, "kind": "test-fail", "status": "open", "run_id": 1, "title": "u"},
                       {"id": 43, "kind": "note", "status": "open", "run_id": None, "title": "v"}])
    with isolated(d, "waitfake", fake={}, FAKE_BRAIN_STATE=state, FAKE_BRAIN_LOG=log):
        rid = run("run-add", "--project", "wp", "--test-project", "W.csproj", "--tier", "full")[1]
        wj = run("queue", "add", repo, "WB.csproj")[1]
        run("queue", "finish", wj, "--status", "done", "--run-id", rid)
        rc, out, _ = run("queue", "wait", wj)
        if rc != 0 or "items=#41,#42" not in out:
            fails.append(f"wait must list the item ids brain holds for the run: {rc} {out!r}")


def session_checks(fails, d):
    os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "none.db")
    os.environ["LEAN_TESTS_DB"] = os.path.join(d, "session.db")
    with isolated(d, "sess-none"):
        if tq.job_session() != ("", ""):
            fails.append(f"job_session with no brain must return empty strings: {tq.job_session()}")
        os.environ.update(LEAN_SESSION_ID="env-id", LEAN_SESSION_CWD="/env/cwd")
        if tq.job_session() != ("env-id", "/env/cwd"):
            fails.append(f"job_session must return the environment values: {tq.job_session()}")
    log = os.path.join(d, "sess-log")
    with isolated(d, "sess-fake", fake={}, FAKE_BRAIN_STATE=os.path.join(d, "sess-state.json"),
                  FAKE_BRAIN_LOG=log):
        got = tq.job_session()
        if got != ("fake-session", os.getcwd()):
            fails.append(f"job_session must return what session infer gave: {got}")
        if ["session", "infer", "--json"] not in logged(log):
            fails.append("job_session must call session infer --json")
        os.environ.update(LEAN_SESSION_ID="env-id", LEAN_SESSION_CWD="/env/cwd")
        n = len(logged(log))
        if tq.job_session() != ("env-id", "/env/cwd") or len(logged(log)) != n:
            fails.append("the environment must win and brain must not be asked")
        os.environ.pop("LEAN_SESSION_ID")
        os.environ.pop("LEAN_SESSION_CWD")
        jid = run("queue", "add", os.path.join(d, "r"), "S.csproj", "--infer-session")[1]
        row = store.connect().execute("SELECT session_id, session_cwd FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row or row["session_id"] != "fake-session":
            fails.append(f"queue add --infer-session must store the inferred session: {row and tuple(row)}")
    with isolated(d, "sess-slow", fake={"FAKE_BRAIN_SLEEP": "8"}):
        t0 = time.time()
        got = tq.job_session()
        if got != ("", "") or time.time() - t0 > 7.5:
            fails.append(f"a slow brain must not hang job_session: {got} after {time.time() - t0:.1f}s")


def flaky_checks(fails, d):
    os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "none.db")
    os.environ["LEAN_TESTS_DB"] = os.path.join(d, "flaky.db")
    state, log = os.path.join(d, "flaky-state.json"), os.path.join(d, "flaky-log")
    seen = "2026-01-01T00:00:00Z"
    fake_state(state, [
        {"id": 1, "kind": "flaky", "status": "open", "project": "core", "run_id": "1", "ref": "A.One",
         "title": "A.One", "last_seen": seen},
        {"id": 2, "kind": "flaky", "status": "open", "project": "core", "run_id": "2", "ref": "B.One",
         "title": "B.One", "last_seen": seen},
        {"id": 3, "kind": "test-fail", "status": "open", "project": "core", "run_id": "1", "ref": "A.Real",
         "title": "A.Real", "last_seen": seen}])
    closes = lambda: [a for a in logged(log) if a[:1] == ["close"]]  # noqa: E731
    with isolated(d, "flaky", fake={}, FAKE_BRAIN_STATE=state, FAKE_BRAIN_LOG=log):
        def frun(test, started, rc, tier="full", passed=3, failed=0):
            return run("run-add", "--project", "core", "--test-project", test, "--tier", tier, "--started",
                       str(started), "--exit-code", str(rc), "--passed", str(passed), "--failed", str(failed))[1]
        if frun("A.csproj", 1000, 1, failed=1) != "1" or frun("B.csproj", 1000, 1, failed=1) != "2":
            fails.append("run-add must print the run ids")
        for i in range(5):  # all before the item was last seen
            frun("A.csproj", 1001 + i, 0)
        rc, out, _ = run("flaky-clear", "--project", "core", "--test-project", "A.csproj")
        if rc != 0 or out or closes():
            fails.append(f"passing runs older than the last sighting must not clear it: {rc} {out!r} {closes()}")
        future = 4102444800  # year 2100
        for i in range(4):
            frun("A.csproj", future + i, 0)
        frun("A.csproj", future + 9, 1, failed=1)
        frun("A.csproj", future + 10, 0, tier="rerun")
        frun("B.csproj", future + 11, 0)
        run("flaky-clear", "--project", "core", "--test-project", "A.csproj")
        if closes():
            fails.append(f"4 passing runs must not clear it: {closes()}")
        frun("A.csproj", future + 12, 0)
        rc, out, _ = run("flaky-clear", "--project", "core", "--test-project", "A.csproj")
        got = closes()
        if len(got) != 1 or got[0][:4] != ["close", "1", "--resolution", "flaky-cleared"] \
                or "5 passing runs" not in " ".join(got[0]) or "closed: #1" not in out:
            fails.append(f"5 passing runs must close the flaky item through brain: {rc} {out!r} {got}")
        run("flaky-clear", "--project", "core", "--test-project", "A.csproj")
        if len(closes()) != 1:
            fails.append("an item already closed must not be closed again, nor another test project's or kind's")
        if run("flaky-clear", "--project", "core", "--test-project", "B.csproj", "--runs", "0")[0] == 0:
            fails.append("--runs 0 must be refused")
    with isolated(d, "flaky-none"):
        rc, out, err = run("flaky-clear", "--project", "core", "--test-project", "A.csproj")
        if (rc, out) != (0, ""):
            fails.append(f"flaky-clear with no brain must do nothing: {rc} {out!r} {err!r}")


def stats_checks(fails, d):
    os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "none.db")
    os.environ["LEAN_TESTS_DB"] = os.path.join(d, "stats.db")
    state, log = os.path.join(d, "stats-state.json"), os.path.join(d, "stats-log")
    fake_state(state, [
        {"id": 1, "kind": "flaky", "status": "closed", "project": "core", "run_id": "2", "title": "x"},
        {"id": 2, "kind": "test-fail", "status": "open", "project": "core", "run_id": "2", "title": "y"},
        {"id": 3, "kind": "flaky", "status": "open", "project": "core", "run_id": "9", "title": "gone"}])
    with isolated(d, "stats", fake={}, FAKE_BRAIN_STATE=state, FAKE_BRAIN_LOG=log):
        for tier, dur, wait in (("full", 100, 10), ("full", 300, 30), ("rerun", 5, 0)):
            run("run-add", "--cycle", "c1", "--project", "core", "--test-project", "T.csproj", "--tier", tier,
                "--runner", "mtp", "--started", "1000", "--ended", str(1000 + dur), "--lane-wait", str(wait),
                "--passed", "9", "--failed", "1", "--exit-code", "1")
        rc, out, _ = run("stats", "--json")
        try:
            s = json.loads(out)
        except ValueError:
            s = {}
            fails.append(f"stats --json is not json: {out!r}")
        suite = (s.get("suites") or [{}])[0]
        if suite.get("runs") != 2 or suite.get("mean_duration_s") != 200 or suite.get("mean_lane_wait_s") != 20 \
                or suite.get("flaky_items") != 1 or suite.get("flaky_rate") != 0.5:
            fails.append(f"suite stats wrong (reruns are not runs, closed flaky items count): {suite}")
        if (s.get("slowest") or [{}])[0].get("duration_s") != 300:
            fails.append(f"slowest wrong: {s.get('slowest')}")
        if ["list", "--json", "--kind", "flaky"] not in logged(log):
            fails.append(f"stats must ask brain for every flaky item: {logged(log)}")
        rc, out, _ = run("stats")
        if "flaky rate" not in out or "T.csproj" not in out or "slowest runs" not in out:
            fails.append(f"stats table: {out!r}")
    with isolated(d, "stats-none"):
        s = json.loads(run("stats", "--json")[1])
        suite = (s.get("suites") or [{}])[0]
        if suite.get("flaky_items") != "brain not installed" or suite.get("flaky_rate") != "brain not installed" \
                or suite.get("runs") != 2 or not s.get("slowest"):
            fails.append(f"stats with no brain must say so for flaky items and rate: {suite}")


def refused_checks(fails, d):
    brain = os.path.join(d, "refuse-brain.db")
    make_brain(brain, 9, [(1, "queued"), (2, "running")], [])
    os.environ["LEAN_BRAIN_DB"] = brain
    os.environ["LEAN_TESTS_DB"] = os.path.join(d, "refuse.db")
    want = ("test-phase: brain.db still has 2 queued or running jobs. "
            "Drain them with lean-agent-tools 1.x, or run `test-phase queue import --cancel-live` "
            "to copy them as cancelled.")
    with isolated(d, "refuse"):
        for argv in (("queue", "list"), ("stats",), ("run-add", "--project", "p", "--test-project", "t",
                                                    "--tier", "full")):
            rc, out, err = run(*argv)
            if rc != 2 or err != want or out:
                fails.append(f"{argv[0]} while the copy is refused must exit 2 with the message: {rc} {err!r}")


def upgrade_checks(fails, d):
    """The way out for a 1.x upgrader: queue import, with and without --cancel-live, and a newer tests.db."""
    live = os.path.join(d, "up-live.db")
    make_brain(live, 9, [(1, "queued"), (2, "running")], [])
    os.environ["LEAN_BRAIN_DB"] = live
    tests = os.path.join(d, "up-live-tests.db")
    os.environ["LEAN_TESTS_DB"] = tests
    with isolated(d, "up-live"):
        rc, out, err = run("queue", "import")
        if rc != 2 or len(err.splitlines()) != 1 or err.count("test-phase: ") != 1 or "--cancel-live" not in err:
            fails.append(f"queue import with live jobs must exit 2 with one line naming --cancel-live: {rc} {err!r}")
        chk = sqlite3.connect(tests)
        left = chk.execute("SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM meta WHERE key=?)",
                           (store.MARKER,)).fetchone()
        chk.close()
        if tuple(left) != (0, 0):
            fails.append(f"a refused queue import must leave no jobs and no marker: {tuple(left)}")
        before = sha(live)
        rc, out, err = run("queue", "import", "--cancel-live")
        if rc != 0:
            fails.append(f"queue import --cancel-live must exit 0: {rc} {err!r}")
        chk = sqlite3.connect(tests)
        rows = chk.execute("SELECT status, reason, ended_at FROM jobs ORDER BY id").fetchall()
        marker = chk.execute("SELECT COUNT(*) FROM meta WHERE key=?", (store.MARKER,)).fetchone()[0]
        chk.close()
        if len(rows) != 2 or any(r[0] != "cancelled" or r[1] != "queued under 1.x, not run" or not r[2] for r in rows):
            fails.append(f"--cancel-live must copy both jobs as cancelled with the reason and ended_at: {rows}")
        if marker != 1:
            fails.append("--cancel-live must set the marker")
        if sha(live) != before:
            fails.append("--cancel-live must never write brain.db")
        rc, out, err = run("queue", "list")
        if rc != 0:
            fails.append(f"queue list after --cancel-live must exit 0: {rc} {err!r}")

    done = os.path.join(d, "up-done.db")
    make_brain(done, 9, [(1, "done"), (2, "failed")], [1, 2])
    os.environ["LEAN_BRAIN_DB"] = done
    tests = os.path.join(d, "up-done-tests.db")
    os.environ["LEAN_TESTS_DB"] = tests
    with isolated(d, "up-done"):
        rc, out, err = run("queue", "import")
        con = sqlite3.connect(tests)
        copied = con.execute("SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM runs)").fetchone()
        con.close()
        if rc != 0 or tuple(copied) != (2, 2):
            fails.append(f"queue import with only finished jobs must copy them and exit 0: {rc} {tuple(copied)} {err!r}")
        rc, out, err = run("queue", "import")
        if rc != 0 or err:
            fails.append(f"a second queue import must exit 0 quietly: {rc} {err!r}")

    newer = os.path.join(d, "up-newer-tests.db")
    con = sqlite3.connect(newer)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    con.commit()
    con.close()
    os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "up-none.db")
    os.environ["LEAN_TESTS_DB"] = newer
    with isolated(d, "up-newer"):
        rc, out, err = run("queue", "list")
        if rc != 2 or len(err.splitlines()) != 1 or "schema" not in err or "Traceback" in err:
            fails.append(f"a newer tests.db schema must exit 2 with one line naming the schema: {rc} {err!r}")


DOTNET_SHIM = ('#!/bin/sh\necho "$*" >> "$FAKE_LOG"\n[ "$1" = build ] && exit 0\n'
               'f="$FAKE_DIR/out-$(basename "$2")"\ncat "$f"\nexit "$(cat "$f.rc")"\n')
TEST_PHASE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          "tools", "test-phase")


def e2e_brain_checks(fails, d):
    """One drain against the real brain named by LEAN_E2E_BRAIN. Unset, the check is skipped with a line."""
    path = os.environ.get("LEAN_E2E_BRAIN")
    if not path:
        print("skipped: the end to end drain against a real brain (LEAN_E2E_BRAIN is not set)")
        return
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        fails.append(f"LEAN_E2E_BRAIN names a file that does not exist: {path}")
        return
    base = os.path.join(d, "e2e")
    shims, bin_dir, home = (os.path.join(base, n) for n in ("shims", "bin", "home"))
    for folder in (shims, bin_dir, home, os.path.join(base, "repo", "tests", "T")):
        os.makedirs(folder)
    with open(os.path.join(shims, "dotnet"), "w") as fh:
        fh.write(DOTNET_SHIM)
    os.chmod(os.path.join(shims, "dotnet"), 0o755)
    os.symlink(path, os.path.join(bin_dir, "brain"))
    tools = os.path.join(base, "tools")
    os.makedirs(tools)
    for where in os.environ.get("PATH", "").split(os.pathsep):
        try:
            names = os.listdir(where) if where and not where.startswith(base) else []
        except OSError:
            continue
        for name in names:
            full, link = os.path.join(where, name), os.path.join(tools, name)
            if name != "brain" and not os.path.lexists(link) and os.path.isfile(full) and os.access(full, os.X_OK):
                os.symlink(full, link)
    with open(os.path.join(base, "out-Fx.csproj"), "w") as fh:
        fh.write("  Failed Fx.Tests.Breaks [1 ms]\n   Assert.Equal() Failure\n"
                 "Failed!  - Failed: 1, Passed: 0, Skipped: 0, Total: 1\n")
    with open(os.path.join(base, "out-Fx.csproj.rc"), "w") as fh:
        fh.write("1")
    plan = os.path.join(base, "plan.txt")
    with open(plan, "w") as fh:
        fh.write(f"{os.path.join(base, 'repo')} tests/T/Fx.csproj\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_BRAIN", "LEAN_E2E"))}
    env.update(PATH=os.pathsep.join([shims, bin_dir, tools]), HOME=home, FAKE_DIR=base,
               FAKE_LOG=os.path.join(base, "calls.log"), LEAN_BRAIN_DB=os.path.join(base, "brain.db"),
               LEAN_TESTS_DB=os.path.join(base, "tests.db"), LEAN_DB=os.path.join(base, "lean.db"),
               HEAVY_LOCK_DIR=os.path.join(base, "locks"), LEAN_TEST_PHASE_DIR=os.path.join(base, "runs"),
               HEAVY_WAIT="20", TEST_PHASE_REMOTES="off")
    env.pop("TEST_PHASE_RUNNER", None)
    r = subprocess.run([sys.executable, TEST_PHASE, plan], env=env, capture_output=True, text=True, timeout=240)
    if r.returncode != 1 or "Traceback" in r.stdout + r.stderr:
        fails.append(f"e2e drain of a failing job must exit 1 with no traceback ({r.returncode}): {r.stdout} {r.stderr}")
    listed = subprocess.run([sys.executable, path, "list", "--json", "--kind", "test-fail"], env=env,
                            capture_output=True, text=True, timeout=60)
    try:
        items = json.loads(listed.stdout)
    except ValueError:
        items = None
    if not isinstance(items, list) or len(items) != 1 or "Fx.Tests.Breaks" not in json.dumps(items[0]):
        fails.append(f"brain list --json --kind test-fail must show the failure the drain filed: "
                     f"{listed.stdout!r} {listed.stderr!r}")


def self_test():
    fails = []
    saved_env = dict(os.environ)
    with tempfile.TemporaryDirectory() as d:
        brain = os.path.join(d, "brain.db")
        os.environ["LEAN_BRAIN_DB"] = brain
        n = [0]

        def fresh_tests_db():
            n[0] += 1
            os.environ["LEAN_TESTS_DB"] = os.path.join(d, f"tests{n[0]}.db")
            return os.environ["LEAN_TESTS_DB"]

        def reset_brain():
            if os.path.exists(brain):
                os.remove(brain)

        # a fresh tests.db, no brain.db
        reset_brain()
        path = fresh_tests_db()
        if store.db_path() != path or store.brain_db_path() != brain:
            fails.append("db_path and brain_db_path must read the environment at call time")
        con = store.connect()
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"jobs", "runs", "meta"} <= names:
            fails.append(f"fresh tests.db must have jobs, runs and meta: {names}")
        ver = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not ver or ver[0] != "1" or store.SCHEMA_VERSION != 1:
            fails.append(f"schema_version must be 1: {ver and ver[0]}")
        if not isinstance(con, sqlite3.Connection) or con.row_factory is not sqlite3.Row:
            fails.append("connect must return a Row connection")
        if count(con, "jobs") or count(con, "runs"):
            fails.append("a tests.db with no brain.db must be empty")
        con.close()

        # columns equal those of the literal version 9 shape
        ref = os.path.join(d, "ref9.db")
        make_brain(ref, 9, [], [])
        for table in ("jobs", "runs"):
            if columns(path, table) != columns(ref, table):
                fails.append(f"{table} columns differ from the version 9 brain.db: "
                             f"{columns(path, table)} vs {columns(ref, table)}")
        if "branch" not in columns(path, "jobs"):
            fails.append("jobs must have a branch column")

        # a brain.db that shows up after the first connect is not copied
        make_brain(brain, 9, [(1, "done")], [1])
        con = store.connect()
        if count(con, "jobs") or count(con, "runs"):
            fails.append("the copy must run once: a later brain.db must not be copied")
        con.close()

        # version 9 with 3 done jobs and 2 runs
        reset_brain()
        make_brain(brain, 9, [(1, "done"), (2, "done"), (7, "failed")], [3, 4])
        before = sha(brain)
        fresh_tests_db()
        con = store.connect()
        if count(con, "jobs") != 3 or count(con, "runs") != 2:
            fails.append(f"version 9 copy must bring 3 jobs and 2 runs: {count(con, 'jobs')} {count(con, 'runs')}")
        ids = [r[0] for r in con.execute("SELECT id FROM jobs ORDER BY id")]
        rids = [r[0] for r in con.execute("SELECT id FROM runs ORDER BY id")]
        if ids != [1, 2, 7] or rids != [3, 4]:
            fails.append(f"copy must keep ids: jobs {ids} runs {rids}")
        if not con.execute("SELECT 1 FROM meta WHERE key='copied_from_brain'").fetchone():
            fails.append("copy must write the copied_from_brain marker")
        con.close()
        if sha(brain) != before:
            fails.append("copying must not change brain.db bytes")
        con = store.connect()
        if count(con, "jobs") != 3:
            fails.append("a second connect must not copy again")
        con.close()

        # a brain.db with neither jobs nor runs only writes the marker
        reset_brain()
        b = sqlite3.connect(brain)
        b.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        b.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, project TEXT)")
        b.commit()
        b.close()
        before = sha(brain)
        fresh_tests_db()
        con = store.connect()
        if count(con, "jobs") or count(con, "runs"):
            fails.append("a brain.db without jobs or runs must copy nothing")
        if not con.execute("SELECT 1 FROM meta WHERE key='copied_from_brain'").fetchone():
            fails.append("a brain.db without jobs or runs must still write the marker")
        con.close()
        if sha(brain) != before:
            fails.append("a brain.db without jobs or runs must keep its bytes")
        # a current tests.db opens again without copying
        make_brain_late = os.path.join(d, "late.db")
        make_brain(make_brain_late, 9, [(1, "done")], [1])
        os.replace(make_brain_late, brain)
        con = store.connect()
        if count(con, "jobs") or count(con, "runs"):
            fails.append("a second connect on a current tests.db must copy nothing")
        con.close()

        # a path with ? and # in it
        odd = os.path.join(d, "odd?x#y")
        os.mkdir(odd)
        os.environ["LEAN_BRAIN_DB"] = os.path.join(odd, "brain.db")
        make_brain(os.environ["LEAN_BRAIN_DB"], 9, [(1, "done")], [1])
        fresh_tests_db()
        con = store.connect()
        if count(con, "jobs") != 1:
            fails.append("a brain.db path with ? and # must still copy")
        con.close()
        os.environ["LEAN_BRAIN_DB"] = brain

        # version 4: columns it lacks keep their defaults
        reset_brain()
        make_brain(brain, 4, [(1, "done")], [1])
        fresh_tests_db()
        con = store.connect()
        row = con.execute("SELECT * FROM jobs").fetchone()
        if not row or (row["priority"], row["platform"], row["host"], row["commit_hash"],
                       row["session_id"], row["branch"]) != (0, "", "", "", "", ""):
            fails.append(f"version 4 copy must leave new columns at defaults: {row and tuple(row)}")
        if row and (row["args"], row["run_id"]) != ("", 1):
            fails.append("version 4 copy must keep args and run_id")
        if count(con, "runs") != 1 or con.execute("SELECT host FROM runs").fetchone()[0] != "":
            fails.append("version 4 copy must bring the run with host at its default")
        con.close()

        # a queued and a running job refuse the copy
        reset_brain()
        make_brain(brain, 9, [(1, "queued"), (2, "running")], [])
        path = fresh_tests_db()
        try:
            store.connect()
            fails.append("a live queue must raise CopyRefused")
        except store.CopyRefused as e:
            if e.count != 2:
                fails.append(f"CopyRefused.count must be 2: {e.count}")
        chk = sqlite3.connect(path)
        marker = chk.execute("SELECT COUNT(*) FROM meta WHERE key='copied_from_brain'").fetchone()[0]
        rows = chk.execute("SELECT (SELECT COUNT(*) FROM jobs) + (SELECT COUNT(*) FROM runs)").fetchone()[0]
        chk.close()
        if marker or rows:
            fails.append(f"a refused copy must leave no rows and no marker: marker {marker} rows {rows}")
        b = sqlite3.connect(brain)
        b.execute("UPDATE jobs SET status='done'")
        b.commit()
        b.close()
        con = store.connect()
        if count(con, "jobs") != 2:
            fails.append("after the jobs are done the next connect must copy")
        con.close()

        for checks in (queue_checks, session_checks, flaky_checks, stats_checks, refused_checks, upgrade_checks,
                       e2e_brain_checks):
            try:
                checks(fails, d)
            except Exception as e:  # noqa: BLE001  one crashed group must not hide the others
                fails.append(f"{checks.__name__} crashed: {e!r}")
    os.environ.clear()
    os.environ.update(saved_env)
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print("usage: selftest.py --self-test", file=sys.stderr)
    sys.exit(2)
