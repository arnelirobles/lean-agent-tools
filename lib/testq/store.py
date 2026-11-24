import os
import sqlite3
import sys
import time
from urllib.parse import quote

SCHEMA_VERSION = 1
MARKER = "copied_from_brain"

RUNS_DDL = """CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
    repo TEXT NOT NULL DEFAULT '', test_project TEXT NOT NULL DEFAULT '', runner TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'full', started INTEGER, ended INTEGER, duration_s REAL,
    lane_wait_s REAL, load_start REAL, load_end REAL, passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
    stalled INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, host TEXT NOT NULL DEFAULT '')"""

JOBS_DDL = """CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle TEXT NOT NULL DEFAULT '', project TEXT NOT NULL,
    repo_dir TEXT NOT NULL, test_project TEXT NOT NULL, filter TEXT NOT NULL DEFAULT '',
    tier TEXT NOT NULL DEFAULT 'full', after_id INTEGER REFERENCES jobs(id),
    status TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
    enqueued_by TEXT NOT NULL DEFAULT '', enqueued_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
    run_id INTEGER REFERENCES runs(id), args TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0, platform TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '',
    commit_hash TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
    session_cwd TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '')"""


class CopyRefused(Exception):
    """Raised when the brain.db being copied still has queued or running jobs."""

    def __init__(self, count):
        super().__init__(f"brain.db has {count} queued or running job(s); let them finish, then retry")
        self.count = count


class NewerSchema(Exception):
    """Raised when tests.db was written by a later test-phase."""

    def __init__(self, path, found):
        super().__init__(f"{path} has schema {found}, this test-phase knows {SCHEMA_VERSION}")
        self.path = path
        self.found = found


CANCEL_REASON = "queued under 1.x, not run"


def db_path():
    return os.environ.get("LEAN_TESTS_DB") or os.path.expanduser("~/.local/share/lean/tests.db")


def brain_db_path():
    return os.environ.get("LEAN_BRAIN_DB") or os.path.expanduser("~/.local/share/lean/brain.db")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _has_table(con, table):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def copy_done(path=None):
    """True when the tests.db at path (default: the configured one) already holds the copy marker."""
    path = path or db_path()
    if not os.path.exists(path):
        return False
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


class Copy:
    """What copy_from_brain did. status is "copied" (jobs and runs hold the row counts), "already" (the marker
    was there), "none" (no brain.db at the path) or "unreadable" (brain.db could not be read; error says why)."""

    def __init__(self, status, jobs=0, runs=0, error=""):
        self.status, self.jobs, self.runs, self.error = status, jobs, runs, error


def _read_source(src, con_columns, cancel_live):
    """Read jobs and runs of the brain.db at src in one snapshot. Returns {table: (shared columns, rows)}.
    Raises CopyRefused while src has live jobs, unless cancel_live. src is never written."""
    uri = "file:" + quote(os.path.abspath(src)) + "?mode=ro"
    source = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")  # one snapshot for the live count and the row reads
        if _has_table(source, "jobs") and not cancel_live:
            live = source.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if live:
                raise CopyRefused(live)
        data = {}
        for table in ("runs", "jobs"):  # runs first, jobs.run_id points at it
            if not _has_table(source, table):
                continue
            shared = [c for c in _columns(source, table) if c in con_columns[table]]
            cols = ", ".join(shared)
            data[table] = (shared, [list(r) for r in source.execute(f"SELECT {cols} FROM {table} ORDER BY id")])
        return data
    finally:
        source.close()


def copy_from_brain(con, src, cancel_live=False):
    """Copy jobs and runs from the brain.db at src into con, once. con must be in autocommit mode.
    Returns a Copy. Raises CopyRefused, with nothing written, while src has live jobs, unless cancel_live:
    then each live job lands in con as cancelled. src is never written. A brain.db that cannot be read gives
    status "unreadable" and no marker, so a later run tries again; so does a missing file. Errors on con
    propagate."""
    con.execute("BEGIN IMMEDIATE")
    try:
        if con.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
            con.execute("COMMIT")
            return Copy("already")
        if not os.path.exists(src):  # lost a race with a delete: nothing was read, so no marker
            con.execute("COMMIT")
            return Copy("none")
        con_columns = {t: _columns(con, t) for t in ("runs", "jobs")}
        try:
            data = _read_source(src, con_columns, cancel_live)
        except sqlite3.DatabaseError as e:
            con.execute("ROLLBACK")
            return Copy("unreadable", error=" ".join(str(e).split()))
        counts = {"runs": 0, "jobs": 0}
        for table, (shared, rows) in data.items():
            cols = ", ".join(shared)
            marks = ", ".join("?" for _ in shared)
            for values in rows:
                if cancel_live and table == "jobs" and values[shared.index("status")] in ("queued", "running"):
                    values[shared.index("status")] = "cancelled"
                    values[shared.index("reason")] = CANCEL_REASON
                    if "ended_at" in shared:
                        values[shared.index("ended_at")] = now()
                con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values)
                counts[table] += 1
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (MARKER, now()))
        con.execute("COMMIT")
        return Copy("copied", counts["jobs"], counts["runs"])
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise


def connect(timeout=30, cancel_live=False, report=None):
    """Open tests.db, set it up, and run the one-time copy. A list passed as report gets the Copy appended."""
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        def meta(key):
            try:
                return con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            except sqlite3.OperationalError:  # no meta table yet
                return None

        found = meta("schema_version")
        if found:
            try:
                newer = int(found[0]) > SCHEMA_VERSION
            except ValueError:
                newer = False
            if newer:
                raise NewerSchema(path, found[0])
        if not found:  # a plain read first, so a set up database takes no write lock
            con.execute("BEGIN IMMEDIATE")
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            con.execute(RUNS_DDL)
            con.execute(JOBS_DDL)
            con.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id)")
            con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),))
            con.execute("COMMIT")
        if meta(MARKER):
            result = Copy("already")
        elif not os.path.exists(brain_db_path()):  # nothing to read: no marker, no write lock
            result = Copy("none")
        else:
            result = copy_from_brain(con, brain_db_path(), cancel_live)
        if result.status == "unreadable":
            print(f"test-phase: could not read {brain_db_path()} ({result.error}); the 1.x queue was not copied. "
                  "Fix or move that file, then run test-phase queue import.", file=sys.stderr)
        if report is not None:
            report.append(result)
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
        raise
    con.isolation_level = ""  # callers use `with con:` to open and commit a transaction
    return con
