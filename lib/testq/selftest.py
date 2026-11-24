import hashlib
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from testq import store  # noqa: E402

# Literal DDL copied from lib/brain/store.py (migrations 2, 3, 4, 6, 7, 8, 9 and the branch column of
# ensure_extras). The self-test never imports lib/brain.
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


def self_test():
    fails = []
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
