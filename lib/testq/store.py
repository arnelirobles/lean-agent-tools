import os
import sqlite3
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


def copy_from_brain(con, src):
    """Copy jobs and runs from the brain.db at src into con, once. con must be in autocommit mode.
    Returns the number of rows copied. Raises CopyRefused, with nothing written, while src has live jobs."""
    con.execute("BEGIN IMMEDIATE")
    source = None
    try:
        if con.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
            con.execute("COMMIT")
            return 0
        copied = 0
        if os.path.exists(src):
            uri = "file:" + quote(os.path.abspath(src)) + "?mode=ro"
            source = sqlite3.connect(uri, uri=True)
            source.row_factory = sqlite3.Row
            if _has_table(source, "jobs"):
                live = source.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                if live:
                    raise CopyRefused(live)
            for table in ("runs", "jobs"):  # runs first, jobs.run_id points at it
                if not _has_table(source, table):
                    continue
                shared = [c for c in _columns(source, table) if c in _columns(con, table)]
                cols = ", ".join(shared)
                marks = ", ".join("?" for _ in shared)
                for row in source.execute(f"SELECT {cols} FROM {table} ORDER BY id"):
                    con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row))
                    copied += 1
        con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (MARKER, now()))
        con.execute("COMMIT")
        return copied
    except BaseException:
        con.execute("ROLLBACK")
        raise
    finally:
        if source is not None:
            source.close()


def connect(timeout=30):
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute(RUNS_DDL)
        con.execute(JOBS_DDL)
        con.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id)")
        con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        con.execute("COMMIT")
        if not con.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
            copy_from_brain(con, brain_db_path())
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        con.close()
        raise
    con.isolation_level = ""  # callers use `with con:` to open and commit a transaction
    return con
