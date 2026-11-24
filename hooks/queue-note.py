#!/usr/bin/env python3
"""Hand the session the test jobs of its project at startup and after a compaction.

SessionStart hook (matcher startup|compact). Reads tests.db read-only: running jobs and the queued count
at startup, the queued jobs too after a compaction. Silent when tests.db is missing, locked, old or newer
than this code understands, and it never creates tests.db or copies anything from brain.db.

  queue-note.py --self-test
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import hookkit  # noqa: E402
from testq import store  # noqa: E402
from testq.queue import job_line, project_of  # noqa: E402

HOOK = {
    "events": [
        {"event": "SessionStart", "matcher": "startup|compact", "group": 1, "order": 1},
    ],
    "timeout": 10,
    "mode": "process",
    "pack": "core",
    "guard": False,
    "default": {"strict": "note", "standard": "note"},
}

LIST_MAX = 8
STARTUP_BYTES = 1000
COMPACT_BYTES = 2000


def short(text, n=110):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n - 3] + "..."


def open_ro(path):
    con = sqlite3.connect("file:" + quote(os.path.abspath(path)) + "?mode=ro", uri=True, timeout=1)
    con.row_factory = sqlite3.Row
    return con


def listing(rows, what):
    lines = [short(job_line(r)) for r in rows[:LIST_MAX]]
    if len(rows) > LIST_MAX:
        lines.append(f"+{len(rows) - LIST_MAX} more {what} jobs")
    return lines


def fit(head, lines, limit):
    """The head and as many lines as fit the byte limit; the last line says how many were left out."""
    out = head
    for i, ln in enumerate(lines):
        if len((out + "\n" + ln).encode()) > limit - 40:
            return out + f"\n... {len(lines) - i} more lines, see test-phase queue list"
        out += "\n" + ln
    return out


def build(cwd, compact):
    path = store.db_path()
    if not os.path.isfile(path):
        return ""
    if not os.path.isdir(cwd or ""):
        cwd = os.getcwd()
    project = project_of(cwd)
    try:
        con = open_ro(path)
        try:
            ver = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not ver or ver[0] != str(store.SCHEMA_VERSION):
                return ""
            where = " AND lower(project)=lower(?)"
            running = con.execute("SELECT * FROM jobs WHERE status='running'" + where + " ORDER BY id",
                                  (project,)).fetchall()
            queued = con.execute("SELECT * FROM jobs WHERE status='queued'" + where
                                 + " ORDER BY priority DESC, id", (project,)).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return ""
    lines = listing(running, "running")
    if compact:
        lines += listing(queued, "queued")
    elif queued:
        lines.append(f"{len(queued)} queued jobs")
    if not lines:
        return ""
    return fit(f"Test queue for {project}:", lines, COMPACT_BYTES if compact else STARTUP_BYTES)


def main():
    event = hookkit.read_event()
    text = build(str(event.get("cwd") or ""), event.get("source") == "compact")
    if text:
        hookkit.note("SessionStart", text)
    return 0


def self_test():
    fails = []
    me = os.path.realpath(__file__)
    git = shutil.which("git")
    with tempfile.TemporaryDirectory() as d:
        bin_dir = os.path.join(d, "bin")
        os.mkdir(bin_dir)
        os.symlink(git, os.path.join(bin_dir, "git"))
        home = os.path.join(d, "home")
        os.mkdir(home)
        proj = os.path.join(d, "myproj")
        os.mkdir(proj)
        subprocess.run([git, "init", "-q", proj], check=True)
        db = os.path.join(d, "tests.db")
        env = dict(os.environ, LEAN_TESTS_DB=db, LEAN_BRAIN_DB=os.path.join(d, "brain.db"), PATH=bin_dir,
                   HOME=home, LEAN_PROFILE="strict")
        env.pop("LEAN_TEST_PHASE_DIR", None)

        def hook(source="startup", e=None, raw=None):
            ev = raw if raw is not None else json.dumps({"cwd": proj, "source": source})
            r = subprocess.run([sys.executable, me], input=ev, env=e or env, capture_output=True, text=True)
            text = ""
            if r.stdout.strip():
                text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
            return r.returncode, text, r.stdout, r.stderr

        def add(n, status, project="MyProj", test="t/Run%d.csproj", priority=0):
            con = sqlite3.connect(db, isolation_level=None)
            for i in range(n):
                con.execute("INSERT INTO jobs (project, repo_dir, test_project, status, priority, enqueued_at) "
                            "VALUES (?, '/x', ?, ?, ?, '2026-01-01T00:00:00Z')",
                            (project, test % i if "%d" in test else test, status, priority))
            con.close()

        rc, text, out, err = hook()
        if (rc, out, err) != (0, "", "") or os.path.exists(db):
            fails.append(f"a missing tests.db must print nothing and not be created: {rc} {out!r} {err!r}")
        old = dict(os.environ, LEAN_TESTS_DB=db, LEAN_BRAIN_DB=env["LEAN_BRAIN_DB"])
        saved = {k: os.environ.get(k) for k in ("LEAN_TESTS_DB", "LEAN_BRAIN_DB")}
        os.environ.update({k: old[k] for k in saved})
        try:
            store.connect().close()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        if hook()[1:] != ("", "", ""):
            fails.append("an empty queue must print nothing")
        add(10, "running")
        add(1, "running", project="elsewhere", test="t/Other.csproj")
        add(3, "queued", test="t/Queued%d.csproj")
        rc, text, out, err = hook()
        jobs = [ln for ln in text.splitlines() if ln.startswith("job #")]
        if rc != 0 or len(jobs) != 8 or "+2 more running jobs" not in text or len(text.encode()) > 1000 \
                or len(out.encode()) > 1000 or "Other.csproj" in text:
            fails.append(f"startup must list 8 of 10 running jobs of this project in 1000 bytes: {rc} {text!r}")
        if "3 queued jobs" not in text or "Queued0" in text:
            fails.append(f"startup must count queued jobs without listing them: {text!r}")
        add(1, "queued", test="t/Bumped.csproj", priority=1)
        rc, text, out, err = hook("compact")
        lines = text.splitlines()
        first_queued = next((ln for ln in lines if "queued" in ln and ln.startswith("job #")), "")
        if rc != 0 or "Queued0" not in text or len(text.encode()) > 2000 or "Bumped" not in first_queued:
            fails.append(f"compact must list queued jobs, bumped first, in 2000 bytes: {rc} {text!r}")
        add(30, "queued", test="t/Long%d.csproj")
        add(10, "running", test="t/" + "x" * 150 + "%d.csproj")
        text = hook("compact")[1]
        if len(text.encode()) > 2000 or "more" not in text.splitlines()[-1]:
            fails.append(f"compact must stay within 2000 bytes with long queues: {len(text.encode())}")
        if hook("startup", dict(env, LEAN_PROFILE="off"))[1:] != ("", "", ""):
            fails.append("the off profile must print nothing")
        # A newer schema prints nothing.
        con = sqlite3.connect(db, isolation_level=None)
        con.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        con.close()
        if hook()[1:] != ("", "", ""):
            fails.append("a newer tests.db must print nothing")
        con = sqlite3.connect(db, isolation_level=None)
        con.execute("DELETE FROM meta WHERE key='schema_version'")
        con.close()
        if hook()[1:] != ("", "", ""):
            fails.append("an old tests.db without a schema version must print nothing")
        con = sqlite3.connect(db, isolation_level=None)
        con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        con.close()
        if not hook()[1]:
            fails.append("restoring the schema version must bring the note back")
        # A locked tests.db: the hook gives up inside the timeout and prints nothing.
        locker = sqlite3.connect(db, isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        t0 = time.time()
        rc, text, out, err = hook()
        took = time.time() - t0
        locker.execute("ROLLBACK")
        locker.close()
        if (rc, out) != (0, "") or took > 8:
            fails.append(f"a locked tests.db must print nothing, exit 0 and be fast: {rc} {out!r} {took:.1f}s")
        # A tests.db that is not a database.
        with open(db, "w") as f:
            f.write("not a database")
        if hook()[0] != 0 or hook()[2]:
            fails.append("a broken tests.db must print nothing and exit 0")
        os.remove(db)
        # The note never builds tests.db from brain.db, even when brain.db has a queued job.
        bcon = sqlite3.connect(env["LEAN_BRAIN_DB"], isolation_level=None)
        bcon.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, project TEXT, status TEXT)")
        bcon.execute("INSERT INTO jobs (project, status) VALUES ('myproj', 'queued')")
        bcon.close()
        if hook()[2] or os.path.exists(db):
            fails.append("the note must not create tests.db or copy brain.db")
        if hook(raw="not json")[0] != 0:
            fails.append("a bad event must not fail")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)
