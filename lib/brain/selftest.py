import json
import os
import subprocess
import sys
import tempfile
import time

from .store import KIND, MIGRATIONS, connect, migrate_1, project_of
from . import records


DIGEST_MAX = 2048


def digest_tests(fails, me, d):
    import sqlite3
    db = os.path.join(d, "digest.db")
    env = dict(os.environ, LEAN_BRAIN_DB=db, LEAN_TESTS_DB=db + ".tests")
    env.pop("LEAN_SESSION_ID", None)
    env.pop("LEAN_SESSION_CWD", None)
    repo = os.path.join(d, "demo")
    os.mkdir(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)

    def brain(*args, cwd=None):
        r = subprocess.run([sys.executable, me, *args], env=env, capture_output=True, text=True, cwd=cwd)
        return r.returncode, r.stdout

    if brain("digest", "--all") != (0, "") or os.path.exists(db):
        fails.append("digest must print nothing and make no database when there is none")
    brain("add", "--project", "x", "--kind", "minor", "--ref", "r")
    brain("close", "1", "--resolution", "fixed")
    rc, out = brain("digest", "--all")
    if rc != 0 or "nothing open" not in out:
        fails.append(f"digest on a brain with nothing open: {rc} {out!r}")
    if brain("digest", "--quiet", cwd=repo) != (0, ""):
        fails.append("digest --quiet must print nothing when nothing is open")
    old = "2020-01-01T00:00:00Z"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    con = sqlite3.connect(db)
    rows = [("DEMO", "blocker", "b1", "waiting on a key", "open", now),
            ("Demo", "question", "q1", "which port", "open", now),
            ("demo", "agent", "old", "stale agent here", "open", old),
            ("demo", "agent", "new", "fresh agent here", "open", now),
            ("demo", "agent", "closed", "closed old agent", "closed", old),
            ("other", "blocker", "ob", "other project blocker", "open", now)]
    rows += [("Bulk%d" % (i % 40), "test-fail", "t%d" % i, "title " + "x" * 80, "open", now) for i in range(2000)]
    con.executemany("INSERT INTO items (project, kind, ref, title, status, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,?,?)", [r + (r[5],) for r in rows])
    rows = [("deep", "blocker", "d%d" % i, "deep blocker %d" % i, "open", now, now) for i in range(10)]
    rows += [("wide", k, "w%s%d" % (k, i), "wide %s %d %s" % (k, i, "y" * 80), "open", old if k == "agent" else now,
              old if k == "agent" else now) for k in ("blocker", "question", "agent") for i in range(8)]
    con.executemany("INSERT INTO items (project, kind, ref, title, status, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,?,?)", [r[:5] + (r[5], r[6]) for r in rows])
    con.commit()
    con.close()
    rc, out = brain("digest", cwd=repo)
    if rc != 0 or "waiting on a key" not in out or "which port" not in out:
        fails.append(f"digest must list blockers and questions, whatever the case of the project: {rc} {out!r}")
    if "other project blocker" in out or "bulk" in out:
        fails.append(f"digest of the current project must not show other projects: {out!r}")
    if "stale agent here" not in out or "fresh agent here" in out or "closed old agent" in out:
        fails.append(f"digest must show only open agents older than 24 hours as stale: {out!r}")
    if "demo:" not in out or "blocker 1" not in out:
        fails.append(f"digest must count by project and kind, with the project in one case: {out!r}")
    if "job #" in out or "queued" in out or "running" in out:
        fails.append(f"digest must print no job lines: {out!r}")
    rc, rec = brain("digest", "--recover", cwd=repo)
    if "job #" in rec or rec.count("stale agent here") != 1 or "fresh agent here" not in rec:
        fails.append(f"--recover must list open agents once, not repeat the digest, and no jobs: {rec!r}")
    rc, rec = brain("digest", "--project", "deep", "--recover")
    plain = brain("digest", "--project", "deep")[1]
    if "deep blocker 0" not in rec or "deep blocker 9" not in rec or "deep blocker 0" in plain:
        fails.append(f"--recover lists every open blocker, the digest only 8: {rec!r}")
    if len(rec.encode()) > 4000:
        fails.append(f"--recover stays under 4 KB: {len(rec.encode())}")
    wide = brain("digest", "--project", "wide")[1]
    if "... cut, see brain list" not in wide or len(wide.encode()) > DIGEST_MAX or "stale agent" not in wide:
        fails.append(f"digest over 2 KB must cut the blockers and questions, not the stale agents: {len(wide)} {wide!r}")
    rc, out = brain("digest", "--project", "DEMO")
    if "waiting on a key" not in out:
        fails.append(f"--project must match without regard to case: {out!r}")
    rc, out = brain("digest", "--all")
    if len(out.encode()) > 2048 or "bulk0" not in out or "stale agent" not in out:
        fails.append(f"digest --all on 2000 items: {len(out.encode())} bytes, {out[:300]!r}")
    if brain("digest", "--all", "--project", "x")[0] == 0:
        fails.append("digest must refuse --all with --project")
def connect_rows(path, query):
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


def serve_tests(fails, me, d):
    import http.server
    import socket
    import threading
    from . import cli
    home = os.path.join(d, "home")
    env = dict(os.environ, LEAN_BRAIN_DB=os.path.join(d, "serve.db"), LEAN_TESTS_DB=os.path.join(d, "serve-tests.db"), HOME=home, USERPROFILE=home,
               APPDATA=os.path.join(home, "AppData"), XDG_CONFIG_HOME=os.path.join(home, "xdg"))
    for name in ("LEAN_SERVE_PORT", "CLAUDE_PLUGIN_OPTION_SERVE_MODE", "CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS"):
        env.pop(name, None)

    def free():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def brain(*args):
        r = subprocess.run([sys.executable, me, *args], env=env, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    class Fake(http.server.BaseHTTPRequestHandler):
        body = b"lean-brain 0"

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.body)

        def log_message(self, *a):
            pass

    def fake(body):
        Fake.body = body
        srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    srv = fake(b"lean-brain 0")
    port = srv.server_address[1]
    rc, out, _ = brain("serve", "--port", str(port))
    if rc != 0 or f"http://leanagent.localhost:{port}" not in out or f"http://127.0.0.1:{port}" not in out:
        fails.append(f"serve where the page answers must print the urls and exit 0: {rc} {out!r}")
    srv.shutdown()
    srv.server_close()
    from .web import server
    held = server.make_server(0, "k")
    try:
        try:
            server.make_server(held.server_address[1], "k")
            fails.append("a second server on a held port must fail to bind")
        except OSError:
            pass
    finally:
        held.server_close()
    rc, out, err = brain("serve", "--port", "99999")
    if rc != 2 or len(err.splitlines()) != 1:
        fails.append(f"a bad port is a one-line error, exit 2: {rc} {err!r}")
    srv = fake(b"something else")
    port = srv.server_address[1]
    rc, out, err = brain("serve", "--port", str(port))
    if rc != 1 or f"port {port} is in use" not in err or out:
        fails.append(f"serve on a port held by something else: {rc} {out!r} {err!r}")
    srv.shutdown()
    srv.server_close()
    port = free()
    p = subprocess.Popen([sys.executable, me, "serve", "--port", str(port)], env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline()
        if f"http://leanagent.localhost:{port}" not in line or f"http://127.0.0.1:{port}" not in line:
            fails.append(f"serve must print both urls: {line!r}")
        if not cli.healthz(port, 5):
            fails.append("a started serve must answer /healthz")
    finally:
        p.terminate()
        p.wait(10)
    db = os.path.join(d, "idle.db")
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, started_at TEXT, ended_at TEXT NOT NULL DEFAULT '')")
    stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    con.execute("INSERT INTO sessions VALUES ('a', '/x', ?, '')", (stamp(10 ** 7 + 99000),))
    con.commit()
    B = 10 ** 7
    st = {}
    if cli.idle_watch(db, 30, st, now=B) or cli.idle_watch(db, 30, st, now=B + 99999):
        fails.append("idle-exit must not fire while a session is open")
    con.execute("UPDATE sessions SET ended_at='t2'")
    con.commit()
    if cli.idle_watch(db, 30, st, now=B + 99999 + 1799) or not cli.idle_watch(db, 30, st, now=B + 99999 + 1800):
        fails.append("idle-exit must fire 30 minutes after the last open session")
    con.execute("UPDATE sessions SET ended_at='', started_at=?", (stamp(B + 99999 + 4000),))
    con.commit()
    if cli.idle_watch(db, 30, st, now=B + 99999 + 5000):
        fails.append("an open session must reset the idle clock")
    # a session that never wrote ended_at (crash, closed terminal) stops counting after 12 hours
    con.execute("UPDATE sessions SET started_at=?", (stamp(B),))
    con.commit()
    st = {}
    if cli.idle_watch(db, 30, st, now=B + 12 * 3600 - 1) or cli.idle_watch(db, 30, st, now=B + 12 * 3600 + 1000):
        fails.append("a session under 12 hours old must count as open")
    st = {}
    if cli.idle_watch(db, 30, st, now=B + 12 * 3600 + 1) or not cli.idle_watch(db, 30, st, now=B + 12 * 3600 + 1 + 1800):
        fails.append("a session open for over 12 hours must not keep the server running")
    con.execute("BEGIN EXCLUSIVE")
    if cli.idle_watch(db, 30, {"last": 0}, now=10 ** 6):
        fails.append("a locked database must count as a session open")
    con.rollback()
    if cli.idle_watch(os.path.join(d, "missing", "x.db"), 30, {"last": 0}, now=10 ** 6):
        fails.append("an unreadable database must count as a session open")
    con.close()
    rc, out, _ = brain("serve", "--uninstall")
    if rc != 0 or "no login unit" not in out:
        fails.append(f"uninstall with no unit: {rc} {out!r}")


def run(brain_cli):
    fails = []
    me = brain_cli
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, LEAN_BRAIN_DB=os.path.join(d, "sub", "brain.db"),
                   LEAN_TESTS_DB=os.path.join(d, "tests.db"))
        env.pop("LEAN_SESSION_ID", None)
        env.pop("LEAN_SESSION_CWD", None)

        def run(*args, stdin=None):
            r = subprocess.run([sys.executable, me, *args], env=env, capture_output=True, text=True, input=stdin)
            return r.returncode, r.stdout.strip(), r.stderr.strip()

        rc, out, _ = run("add", "--project", "core", "--branch", "main", "--kind", "test-fail", "--ref", "Foo.Bar",
                         "--body", "boom", "--severity", "important", "--tag", "db")
        if rc != 0 or not out.startswith("added: #1 open test-fail core@main Foo.Bar important occ=1") \
                or "tags=db" not in out:
            fails.append(f"add: {rc} {out!r}")
        rc, out, _ = run("add", "--project", "core", "--kind", "test-fail", "--ref", "Foo.Bar", "--tag", "load")
        if "seen again: #1" not in out or "occ=2" not in out or "tags=db,load" not in out:
            fails.append(f"dedupe must bump occurrences and merge tags: {out!r}")
        rc, out, _ = run("show", "1")
        if "body: boom" not in out or "core@main" not in out:
            fails.append(f"a repeat without body or branch lost them: {out!r}")
        run("add", "--project", "core", "--kind", "flaky", "--ref", "Foo.Bar")
        rc, out, _ = run("add", "--project", "web", "--kind", "question", "--ref", "x", "--body-file", "-",
                         "--model", "sonnet", stdin="line one\nline two")
        if rc != 0:
            fails.append(f"add with body on stdin: {out!r}")
        rc, out, _ = run("show", "3")
        if "line one" not in out or "model: sonnet" not in out:
            fails.append(f"stdin body or model lost: {out!r}")
        rc, out, _ = run("list")
        if len(out.splitlines()) != 3:
            fails.append(f"kind is part of the dedupe key, want 3 rows: {out!r}")
        for args, want in ((("--project", "core", "--kind", "flaky"), 1), (("--tag", "db"), 1), (("--tag", "nope"), 0),
                           (("--status", "open"), 3), (("--cycle", "zz"), 0)):
            rc, out, _ = run("list", *args)
            if len([l for l in out.splitlines() if l]) != want:
                fails.append(f"list {args}: want {want} got {out!r}")
        rc, out, _ = run("close", "1", "--resolution", "fixed", "--commit", "abc123", "--note", "fixed in abc", "--tag", "done")
        if rc != 0 or "closed: #1 closed" not in out or "done" not in out:
            fails.append(f"close: {rc} {out!r}")
        rc, out, _ = run("show", "1")
        if "fixed in abc" not in out or "resolution: fixed" not in out or "commit: abc123" not in out:
            fails.append(f"close fields missing from show: {out!r}")
        rc, out, _ = run("list", "--open")
        if "#1 " in out or len(out.splitlines()) != 2:
            fails.append(f"--open still lists a closed item: {out!r}")
        run("add", "--project", "core", "--kind", "test-fail", "--ref", "Foo.Bar")
        rc, out, _ = run("list", "--project", "core", "--kind", "test-fail")
        if len(out.splitlines()) != 2:
            fails.append(f"a closed item must not absorb a new sighting: {out!r}")
        if run("reopen", "1")[0] == 0:
            fails.append("reopen made a second open copy of the same item")
        run("close", "4", "--wontfix", "--resolution", "not-a-bug")
        rc, out, _ = run("reopen", "4")
        if rc != 0 or "reopened: #4 open" not in out:
            fails.append(f"reopen: {rc} {out!r}")
        if "no item #99" not in run("close", "99")[2]:
            fails.append("close of a missing id")
        if run("add", "--project", "p", "--kind", "note", "--ref", "r", "--severity", "huge")[0] == 0:
            fails.append("unknown severity accepted")
        rc, _, err = run("add", "--project", "p", "--kind", "anything-goes", "--ref", "r")
        if rc == 0 or "test-fail" not in err or "review-finding" not in err:
            fails.append(f"an unknown kind must be refused with the list: {rc} {err!r}")

        # An item still carries a run id, though the runs live in tests.db now.
        run("add", "--project", "core", "--kind", "flaky", "--ref", "Other.Flaky", "--run-id", "2", "--cycle", "c1")
        rc, out, _ = run("show", "5")
        if "run id: 2" not in out:
            fails.append(f"item not linked to its run: {out!r}")
        rc, out, _ = run("stats", "--json")
        try:
            s = json.loads(out)
        except ValueError:
            s = {}
            fails.append(f"stats --json not json: {out!r}")
        if any(k in s for k in ("suites", "slowest")):
            fails.append(f"stats must not carry run data: {sorted(s)}")
        if not all(k in s for k in ("by_kind_status", "open_by_project", "stale_agents", "median_close_s")):
            fails.append(f"stats keys missing: {sorted(s)}")
        if not any(r["kind"] == "flaky" and r["status"] == "open" for r in s.get("by_kind_status", [])) \
                or "test-fail" not in s.get("median_close_s", {}):
            fails.append(f"counts or median close missing: {s}")
        rc, out, _ = run("stats")
        if "open by project" not in out or "suites" in out or "slowest" in out:
            fails.append(f"stats table: {out!r}")
        rc, out, _ = run("export", "--format", "md")
        if not out.startswith("# Brain") or "## core" not in out or "Foo.Bar" not in out:
            fails.append(f"export md: {out!r}")
        rc, out, _ = run("export", "--format", "json", "--status", "open")
        try:
            if not all(i["status"] == "open" and "tags" in i for i in json.loads(out)):
                fails.append("export json filter or tags")
        except ValueError:
            fails.append(f"export json: {out!r}")

        # resume: ordering, the cap, and silence with nothing to say.
        for k, t in (("question", "q1"), ("pending", "p1"), ("flaky", "f1"), ("agent", "a1"), ("blocker", "b1"),
                     ("pending", "p2")):
            run("add", "--project", "res", "--kind", k, "--ref", t, "--title", t)
        run("add", "--project", "other", "--kind", "blocker", "--ref", "x")
        rc, out, _ = run("resume", "--project", "res")
        order = [l.split()[2] for l in out.splitlines()[1:]]
        if rc != 0 or out.splitlines()[0] != "brain: 6 open for res" or order != ["b1", "a1", "p2", "p1", "q1", "f1"]:
            fails.append(f"resume order (blocker, agent, pending newest first, question, rest): {out!r}")
        rc, out, _ = run("resume", "--project", "res", "--max", "5")
        lines = out.splitlines()
        if len(lines) != 5 or "3 more" not in lines[-1] or "--project res --status open" not in lines[-1] \
                or "b1" not in lines[1]:
            fails.append(f"resume cap: {out!r}")
        for i in range(60):
            run("add", "--project", "big", "--kind", "pending", "--ref", f"n{i}")
        if len(run("resume", "--project", "big")[1].splitlines()) != 40:
            fails.append("resume default cap is not 40 lines")
        if run("resume", "--project", "empty")[1:] != ("", ""):
            fails.append("resume with nothing open must print nothing")
        env["LEAN_BRAIN_DB"] = os.path.join(d, "absent.db")
        rc, out, err = run("resume", "--project", "res")
        if (rc, out, err) != (0, "", "") or os.path.exists(env["LEAN_BRAIN_DB"]):
            fails.append(f"resume with no database must print nothing and create nothing: {rc} {out!r} {err!r}")
        env["LEAN_BRAIN_DB"] = os.path.join(d, "sub", "brain.db")

        ver = connect_rows(env["LEAN_BRAIN_DB"], "SELECT value FROM meta WHERE key='schema_version'")[0][0]
        if ver != str(len(MIGRATIONS)):
            fails.append(f"schema version after open: {ver}")

        # A worktree shares its repository's project name.
        main_repo = os.path.join(d, "mainrepo")
        wt = os.path.join(d, "a-worktree")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", main_repo]
        subprocess.run(["git", "init", "-q", main_repo], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
        subprocess.run(git + ["worktree", "add", "-q", wt, "-b", "side"], check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", wt, "commit", "-q", "--allow-empty",
                        "-m", "y"], check=True)
        if project_of(os.path.join(wt, ".")) != "mainrepo" or project_of(os.path.join(d, "nogit")) != "nogit":
            fails.append("project_of for a worktree or a plain directory")

        # Migration: a database from the flat first layout moves forward and keeps its rows.
        import sqlite3 as sq
        old = os.path.join(d, "old.db")
        con = sq.connect(old)
        con.row_factory = sq.Row
        migrate_1(con)
        con.execute("INSERT INTO entries (project, branch, kind, ref, detail, status, seen, first_seen, last_seen,"
                    " closed_at, note) VALUES ('core','b','test-failure','Old.One','why','closed',3,"
                    "'2026-10-01T00:00:00Z','2026-10-02T00:00:00Z','2026-10-03T00:00:00Z','done')")
        con.commit()
        con.close()
        env["LEAN_BRAIN_DB"] = old
        rc, out, _ = run("show", "1")
        if rc != 0 or "test-fail core@b Old.One" not in out or "occ=3" not in out or "closed: done" not in out:
            fails.append(f"migration lost or changed the old row: {rc} {out!r}")
        con = sq.connect(old)
        v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        if v != str(len(MIGRATIONS)):
            fails.append(f"schema version after migrating: {v}")
        run("add", "--project", "core", "--kind", "note", "--ref", "n")
        if run("list")[1].count("\n") != 1:
            fails.append("a migrated database did not take a new item")

        # Two openers racing to migrate a fresh database: one migrates, the other waits and finds it done.
        import threading
        saved = os.environ.get("LEAN_BRAIN_DB")
        os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "race.db")

        def slow(con):
            time.sleep(1.0)
            con.execute("CREATE TABLE slow_t (x)")

        MIGRATIONS.append(slow)
        errors = []

        def opener(delay):
            time.sleep(delay)
            try:
                connect().close()
            except BaseException as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=opener, args=(x,)) for x in (0, 0.3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            fails.append(f"concurrent openers must not collide on a migration: {errors}")
        con = sq.connect(os.environ["LEAN_BRAIN_DB"])
        v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        if v != str(len(MIGRATIONS)):
            fails.append(f"schema version after the race: {v}")

        # A migration that fails halfway leaves the old version and nothing half-built; the next open finishes it.
        os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "partial.db")
        MIGRATIONS.pop()

        def broken(con):
            con.execute("CREATE TABLE half (x)")
            raise RuntimeError("boom")

        MIGRATIONS.append(broken)
        try:
            connect()
            fails.append("a failing migration did not raise")
        except RuntimeError:
            pass
        con = sq.connect(os.environ["LEAN_BRAIN_DB"])
        v = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        half = con.execute("SELECT name FROM sqlite_master WHERE name IN ('half', 'items')").fetchall()
        con.close()
        if v is not None and v[0] != "0" or half:
            fails.append(f"a failed migration must roll back completely: version {v}, tables {half}")
        MIGRATIONS.pop()
        try:
            connect().close()
        except BaseException as e:  # noqa: BLE001
            fails.append(f"recovery after a failed migration: {e!r}")
        if saved is None:
            os.environ.pop("LEAN_BRAIN_DB")
        else:
            os.environ["LEAN_BRAIN_DB"] = saved

        # The session that filed an item is kept, so an answer can go back to it.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "session.db")
        env["LEAN_SESSION_ID"], env["LEAN_SESSION_CWD"] = "abc-123", "/work/repo"
        rc, out, _ = run("add", "--project", "p", "--kind", "question", "--ref", "q1")
        rc, out, _ = run("show", "1")
        if rc != 0 or "session: abc-123" not in out or "session cwd: /work/repo" not in out:
            fails.append(f"add must store the session from the environment: {out!r}")
        rc, out, _ = run("add", "--project", "p", "--kind", "question", "--ref", "q2", "--session", "def-456",
                         "--session-cwd", "/other")
        rc, out, _ = run("show", "2")
        if "session: def-456" not in out or "session cwd: /other" not in out:
            fails.append(f"--session and --session-cwd must win over the environment: {out!r}")
        env.pop("LEAN_SESSION_ID"), env.pop("LEAN_SESSION_CWD")
        run("add", "--project", "p", "--kind", "question", "--ref", "q1")
        rc, out, _ = run("show", "1")
        if "session: abc-123" not in out:
            fails.append(f"a sighting with no session must keep the old one: {out!r}")
        run("add", "--project", "p", "--kind", "question", "--ref", "q1", "--session", "new-1", "--session-cwd", "/n")
        rc, out, _ = run("show", "1")
        if "session: new-1" not in out or "session cwd: /n" not in out:
            fails.append(f"a sighting from another session must take it over: {out!r}")
        run("add", "--project", "p", "--kind", "note", "--ref", "plain")
        rc, out, _ = run("show", "3")
        if "session" in out:
            fails.append(f"an item with no session must not print one: {out!r}")
        rc, _, err = run("add", "--project", "p", "--kind", "question", "--ref", "bad", "--session", "a b;rm")
        if rc == 0 or "session" not in err:
            fails.append(f"a session id with odd characters must be refused: {rc} {err!r}")

        # Session liveness: the extension resumes only a session that has ended.
        rc, out, _ = run("session", "status", "abc-123")
        if (rc, out) != (0, "unknown"):
            fails.append(f"a session never seen is unknown: {rc} {out!r}")
        run("session", "start", "abc-123", "--cwd", "/work/repo")
        rc, out, _ = run("session", "status", "abc-123")
        if out != "open /work/repo":
            fails.append(f"a started session is open: {out!r}")
        run("session", "end", "abc-123")
        if run("session", "status", "abc-123")[1] != "ended /work/repo":
            fails.append("an ended session must say so")
        run("session", "start", "abc-123", "--cwd", "/work/repo")
        if run("session", "status", "abc-123")[1] != "open /work/repo":
            fails.append("a resumed session is open again")
        if run("session", "start", "x;y", "--cwd", "/")[0] == 0:
            fails.append("session start must refuse an odd id")

        # Answers reach the asking session once: by the hook, unless the extension delivered it already.
        run("add", "--project", "p", "--kind", "question", "--ref", "q9", "--title", "which port", "--session", "s-9")
        rc, out, _ = run("answers", "--session", "s-9")
        if (rc, out) != (0, ""):
            fails.append(f"an open question has no answer yet: {rc} {out!r}")
        run("close", "4", "--resolution", "answered", "--note", "use 8080")
        rc, out, _ = run("answers", "--session", "s-9")
        if "#4 which port" not in out or "use 8080" not in out:
            fails.append(f"an answered question must reach its session: {out!r}")
        if run("answers", "--session", "s-9")[1] != "":
            fails.append("an answer must be delivered once")
        run("add", "--project", "p", "--kind", "question", "--ref", "q10", "--session", "s-9")
        run("close", "5", "--resolution", "answered", "--note", "yes", "--delivered")
        if run("answers", "--session", "s-9")[1] != "":
            fails.append("close --delivered must keep the hook from sending it again")
        run("add", "--project", "p", "--kind", "question", "--ref", "q11", "--session", "s-9")
        run("close", "6", "--resolution", "fixed")
        if run("answers", "--session", "s-9")[1] != "":
            fails.append("only questions closed as answered are answers")
        if run("answers", "--session", "other")[1] != "":
            fails.append("answers go only to the session that asked")
        # Activity: tools such as BaryoVM append JSON lines; brain imports each line once.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "activity.db")
        log = os.path.join(d, "activity.jsonl")

        def jl(**kw):
            base = {"ts": "2026-10-19T08:00:00Z", "version": "1.2.0", "action": "stack release", "stack": "app",
                    "vm": "oci1", "ok": True, "duration_ms": 1200, "host": "mac"}
            base.update(kw)
            return json.dumps(base)

        with open(log, "w") as f:
            f.write(jl() + "\n" + jl(ok=False, error="exit 1", action="vm exec") + "\n")
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if rc != 0 or "imported 2" not in out or "skipped 0" not in out:
            fails.append(f"import-baryovm first run: {rc} {out!r}")
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if "imported 0" not in out or run("activity", "list")[1].count("\n") != 1:
            fails.append(f"a second import must add nothing: {out!r}")
        with open(log, "a") as f:
            f.write(jl(action="deploy") + "\n")
        run("activity", "import-baryovm", "--file", log)
        if run("activity", "list")[1].count("\n") != 2:
            fails.append("an appended line must be imported once")
        with open(log, "w") as f:
            f.write(jl(action="rotated") + "\n")
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if "imported 1" not in out or "rotated" not in run("activity", "list")[1]:
            fails.append(f"a file that shrank must be read from the start: {out!r}")
        with open(log, "a") as f:
            f.write("not json\n" + jl(token="abc") + "\n" + jl(ok="yes") + "\n" + jl(action="after-bad") + "\n")
            f.write(jl(action="half"))  # no newline yet: a write in progress
        rc, out, _ = run("activity", "import-baryovm", "--file", log)
        if rc != 0 or "imported 1" not in out or "skipped 3" not in out:
            fails.append(f"bad lines are skipped and counted, not fatal: {rc} {out!r}")
        listing = run("activity", "list")[1]
        if "after-bad" not in listing or "half" in listing or "abc" in listing:
            fails.append(f"a line with unknown fields must not be stored, a partial line waits: {listing!r}")
        with open(log, "a") as f:
            f.write("\n")
        run("activity", "import-baryovm", "--file", log)
        if "half" not in run("activity", "list")[1]:
            fails.append("a partial line must be read once it is complete")
        rc, out, _ = run("activity", "import-baryovm", "--file", os.path.join(d, "missing.jsonl"))
        if rc != 0:
            fails.append(f"a missing activity file is not an error: {rc} {out!r}")
        run("activity", "add", "--source", "agent", "--action", "build", "--project", "p", "--ok", "0",
            "--error", "boom", "--ts", "2020-01-01T00:00:00Z")
        failed = run("activity", "list", "--failed")[1]
        if failed.count("\n") != 1 or "build" not in failed or "boom" not in failed or "after-bad" in failed:
            fails.append(f"--failed lists only failures: {failed!r}")
        if "build" in run("activity", "list", "--since", "24h")[1]:
            fails.append("--since must drop old rows")
        if "build" not in run("activity", "list", "--since", "9999d")[1]:
            fails.append("--since keeps rows inside the window")
        if run("activity", "list", "--source", "agent")[1].count("\n") != 0 \
                or "build" not in run("activity", "list", "--source", "agent")[1] \
                or run("activity", "list", "--project", "nope")[1]:
            fails.append("--source and --project filter")

        # No schema bump: the activity commands leave the schema version where the other commands put it.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "shared.db")
        run("add", "--project", "keepme", "--kind", "note", "--ref", "r")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        before = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        run("activity", "add", "--source", "agent", "--action", "x")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        ver = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        if ver != before or ver != str(len(MIGRATIONS)):
            fails.append(f"activity commands must not bump the schema version: {before} then {ver}")

        # Rotated file that regrows past the stored offset is read from the start.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "rot.db")
        rlog = os.path.join(d, "rot.jsonl")
        with open(rlog, "w") as f:
            f.write("".join(jl(action=f"old{n}") + "\n" for n in range(2)))
        run("activity", "import-baryovm", "--file", rlog)
        with open(rlog, "w") as f:
            f.write("".join(jl(action=f"new{n}", ts=f"2026-10-20T08:00:0{n}Z") + "\n" for n in range(6)))
        rc, out, _ = run("activity", "import-baryovm", "--file", rlog)
        if "imported 6" not in out or "skipped 0" not in out:
            fails.append(f"a regrown rotated file must be read whole: {out!r}")

        # A symlink to the same file is the same file.
        link = os.path.join(d, "link.jsonl")
        os.symlink(rlog, link)
        if "imported 0" not in run("activity", "import-baryovm", "--file", link)[1]:
            fails.append("a symlinked path must share the offset")

        # One bad row never blocks the import.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "bad.db")
        blog = os.path.join(d, "bad.jsonl")
        with open(blog, "w") as f:
            f.write(jl(duration_ms=2 ** 70) + "\n" + jl(duration_ms=-1) + "\n" + jl(duration_ms="5") + "\n"
                    + jl(duration_ms=True) + "\n" + jl(vm=1) + "\n" + jl(stack=None) + "\n" + jl(error=3) + "\n"
                    + jl(host=[]) + "\n" + jl(ts="2026-10-19 08:00:00") + "\n" + jl(ts="2026-10-19T08:00:00+02:00")
                    + "\n" + jl(action="good") + "\n")
            f.write(b"\xff\xfe bad bytes\n".decode("latin-1") + "\n")
        with open(blog, "ab") as f:
            f.write(b'{"ts":"2026-10-19T08:00:00Z","action":"\xff","ok":true}\n')
        rc, out, _ = run("activity", "import-baryovm", "--file", blog)
        if rc != 0 or "imported 1," not in out or "skipped 12" not in out:
            fails.append(f"each bad row is skipped, the good one imported: {rc} {out!r}")
        rc, out, _ = run("activity", "import-baryovm", "--file", blog)
        if "imported 0, skipped 0" not in out:
            fails.append(f"offset must pass the bad rows: {out!r}")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        raw = con.execute("SELECT raw FROM activities").fetchone()[0]
        con.close()
        if raw != jl(action="good"):
            fails.append(f"raw is the line as written: {raw!r}")

        # Unreadable paths say so.
        for bad in (d, os.path.join(d, "nope", "x")):
            rc, out, err = run("activity", "import-baryovm", "--file", bad)
            if bad == d and (rc != 1 or "Traceback" in err or not err):
                fails.append(f"a directory as --file: {rc} {err!r}")
        # add checks --ts like the importer
        rc, _, err = run("activity", "add", "--source", "a", "--action", "b", "--ts", "yesterday")
        if rc == 0 or "Traceback" in err:
            fails.append(f"activity add must refuse a bad --ts: {rc} {err!r}")

        # list is newest first by ts, not by id
        env["LEAN_BRAIN_DB"] = os.path.join(d, "order.db")
        for ts, name in (("2026-01-02T00:00:00Z", "mid"), ("2026-01-03T00:00:00Z", "new"), ("2026-01-01T00:00:00Z", "old")):
            run("activity", "add", "--source", "s", "--action", name, "--ts", ts)
        names = [ln.split()[4] for ln in run("activity", "list")[1].splitlines()]
        if names != ["new", "mid", "old"]:
            fails.append(f"list is newest first by ts: {names}")
        digest_tests(fails, me, d)

        # close still accepts the flaky-cleared resolution that test-phase sets.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "flaky.db")
        run("add", "--project", "core", "--kind", "flaky", "--ref", "A.One")
        rc, out, _ = run("close", "1", "--resolution", "flaky-cleared")
        if rc != 0:
            fails.append(f"close must accept the flaky-cleared resolution: {rc} {out!r}")
        # Project names are stored lower case, so BarakoCMS and barakocms are one project.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "case.db")
        run("add", "--project", "BarakoCMS", "--kind", "pending", "--ref", "x")
        rc, out, _ = run("add", "--project", "barakocms", "--kind", "pending", "--ref", "x")
        if "seen again: #1" not in out or "occ=2" not in out or " barakocms " not in out:
            fails.append(f"a project name must be stored lower case: {out!r}")
        if "#1 " not in run("list", "--project", "BARAKOCMS")[1] or "#1 " not in run("resume", "--project", "BarakoCMS")[1]:
            fails.append("list and resume must match a project in any case")
        if sorted(r["project"] for r in connect_rows(env["LEAN_BRAIN_DB"], "SELECT project FROM items")) != ["barakocms"]:
            fails.append("the stored project must be lower case")

        # Kinds are a fixed list with aliases, and an unknown kind is refused.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "kinds.db")
        for alias, want in (("todo", "pending"), ("task", "pending"), ("pending", "pending"),
                            ("open-question", "question"), ("finding", "review-finding"), ("running", "agent"),
                            ("running-agent", "agent"), ("isolation", "isolation"), ("Risk", "risk")):
            rc, out, err = run("add", "--project", "k", "--kind", alias, "--ref", "ref-" + alias)
            if rc != 0 or f" {want} k ref-{alias}" not in out:
                fails.append(f"kind {alias} must be stored as {want}: {rc} {out!r} {err!r}")
        if "#1 " not in run("list", "--kind", "todo")[1]:
            fails.append("list --kind must accept an alias")
        run("add", "--project", "k", "--kind", "todo", "--ref", "same")
        if "seen again" not in run("add", "--project", "k", "--kind", "task", "--ref", "same")[1]:
            fails.append("two aliases of one kind are the same item")
        for bad in ("82-tests-to-run", "open", "test", ""):
            rc, _, err = run("add", "--project", "k", "--kind", bad, "--ref", "r")
            if rc == 0 or "one of:" not in err:
                fails.append(f"kind {bad!r} must be refused with the list: {rc} {err!r}")

        # close needs a resolution; --wontfix is the other way to say why.
        rc, _, err = run("close", "1")
        if rc == 0 or "--resolution" not in err or "open" not in run("show", "1")[1].splitlines()[0]:
            fails.append(f"close with no resolution must be refused and leave the item open: {rc} {err!r}")
        if run("close", "1", "--resolution", "fixed")[0] != 0:
            fails.append("close with a resolution")
        if run("close", "2", "--wontfix")[0] != 0:
            fails.append("close --wontfix must keep working")
        if " wontfix " not in run("show", "2")[1].splitlines()[0]:
            fails.append("close --wontfix must set the status wontfix")

        # An agent item with no update for 24 hours is stale in stats.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "stale.db")
        run("add", "--project", "s", "--kind", "agent", "--ref", "old-agent", "--title", "old one")
        run("add", "--project", "s", "--kind", "agent", "--ref", "new-agent", "--title", "new one")
        run("add", "--project", "s", "--kind", "pending", "--ref", "old-pending")
        con = sq.connect(env["LEAN_BRAIN_DB"])
        con.execute("UPDATE items SET last_seen='2020-01-01T00:00:00Z' WHERE ref IN ('old-agent', 'old-pending')")
        con.commit()
        con.close()
        st = json.loads(run("stats", "--json")[1])
        if [x["title"] for x in st["stale_agents"]] != ["old one"]:
            fails.append(f"only the old open agent is stale: {st.get('stale_agents')}")
        out = run("stats")[1]
        if "stale agents" not in out or "old one" not in out or "new one" in out.split("stale agents")[1].split("median")[0]:
            fails.append(f"stats must list the stale agent: {out!r}")
        run("close", "1", "--resolution", "answered")
        if json.loads(run("stats", "--json")[1])["stale_agents"]:
            fails.append("a closed agent is not stale")

        # Migration 8 on a copy of a database with the current mix.
        mixed = os.path.join(d, "mixed.db")
        con = sq.connect(mixed)
        con.row_factory = sq.Row
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for m in MIGRATIONS[:7]:
            m(con)
        con.execute("INSERT INTO meta VALUES ('schema_version', '7')")
        t0, t1 = "2026-10-18T00:00:00Z", "2026-10-20T00:00:00Z"

        def item(project, kind, ref, status="open", resolution="", occ=1, first=t0, last=t0, session=""):
            cur = con.execute("INSERT INTO items (project, kind, ref, title, status, resolution, occurrences,"
                              " first_seen, last_seen, session_id, closed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (project, kind, ref, ref, status, resolution, occ, first, last, session,
                               t1 if status != "open" else None))
            return cur.lastrowid
        a1 = item("barakoCMS", "todo", "x", occ=2, first=t0, last=t0)
        a2 = item("barakocms", "pending", "x", occ=3, first="2026-10-19T00:00:00Z", last=t1, session="s-1")
        a3 = item("medXpress", "82-tests-to-run", "t1")
        a4 = item("medxpress", "open", "t2")
        a5 = item("BaryoClub", "test", "t3")
        a6 = item("baryoclub", "weird-kind", "t4")
        a7 = item("core", "running-agent", "ag")
        a8 = item("core", "open-question", "q")
        a9 = item("core", "finding", "f")
        a10 = item("core", "test-fail", "tf1", status="closed")
        a11 = item("Core", "test-fail", "tf2", status="closed", resolution="fixed")
        a12 = item("core", "test-fail", "tf3", status="closed", occ=2)
        a13 = item("Ärger", "defect", "ae")
        con.execute("INSERT INTO tags VALUES (?, 'a')", (a1,))
        con.execute("INSERT INTO tags VALUES (?, 'b')", (a2,))
        con.execute("INSERT INTO tags VALUES (?, 'a')", (a2,))
        con.commit()
        con.close()
        env["LEAN_BRAIN_DB"] = mixed
        rc, out, err = run("list")
        if rc != 0:
            fails.append(f"a database with the old mix must open: {err!r}")
        rows = {r["id"]: r for r in connect_rows(mixed, "SELECT * FROM items")}
        if any(r["project"] != r["project"].lower() for r in rows.values()):
            fails.append("migration must lower case every project")
        if a2 in rows or a1 not in rows:
            fails.append("the two open copies of one item must merge into the oldest row")
        else:
            m = rows[a1]
            if m["occurrences"] != 5 or m["first_seen"] != t0 or m["last_seen"] != t1 or m["kind"] != "pending" \
                    or m["session_id"] != "s-1":
                fails.append(f"merged item must add the counts and span the dates: {dict(m)}")
            tags = [t["tag"] for t in connect_rows(mixed, "SELECT tag FROM tags WHERE item_id=%d ORDER BY tag" % a1)]
            if tags != ["a", "b"] or connect_rows(mixed, "SELECT * FROM tags WHERE item_id=%d" % a2):
                fails.append(f"merge must move the tags: {tags}")
        want = {a3: "pending", a4: "pending", a5: "note", a6: "note", a7: "agent", a8: "question", a9: "review-finding"}
        got = {i: rows[i]["kind"] for i in want if i in rows}
        if got != want:
            fails.append(f"odd kinds must map to the closest allowed kind: {got}")
        if rows[a13]["project"] != "ärger" or rows[a13]["kind"] != "bug":
            fails.append(f"migration must lower case non-ASCII projects and map defect to bug: {dict(rows[a13])}")
        if f"#{a13} " not in run("list", "--project", "ÄRGER")[1]:
            fails.append("list --project must match a non-ASCII project in any case")
        if any(r["kind"] not in KIND for r in rows.values()):
            fails.append("every kind must be on the list after migrating")
        if rows[a10]["resolution"] != "" or rows[a10]["status"] != "closed" or rows[a11]["resolution"] != "fixed" \
                or rows[a12]["occurrences"] != 2:
            fails.append("closed items, with or without a resolution, must come through unchanged")
        if connect_rows(mixed, "SELECT value FROM meta WHERE key='schema_version'")[0]["value"] != str(len(MIGRATIONS)):
            fails.append("migration must record the new version")
        if run("close", str(a1), "--resolution", "fixed")[0] != 0 or run("add", "--project", "Core", "--kind",
                                                                        "todo", "--ref", "after")[0] != 0:
            fails.append("a migrated database must take close and add")

        # Items carry the session of the one that filed them, also when it is found from the folder (MCP) or
        # from the job that test-phase ran.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "infer.db")
        here = os.path.realpath(os.getcwd())
        run("session", "start", "old-1", "--cwd", here)
        run("session", "start", "new-1", "--cwd", here)
        run("session", "start", "far-1", "--cwd", "/somewhere/else")
        run("session", "end", "old-1")
        run("add", "--project", "p", "--kind", "note", "--ref", "plain")
        run("add", "--project", "p", "--kind", "note", "--ref", "inferred", "--infer-session")
        if "session" in run("show", "1")[1] or "session: new-1" not in run("show", "2")[1] \
                or f"session cwd: {here}" not in run("show", "2")[1]:
            fails.append(f"--infer-session takes the open session of this folder: {run('show', '2')[1]!r}")
        run("add", "--project", "p", "--kind", "note", "--ref", "given", "--infer-session", "--session", "mine-1")
        if "session: mine-1" not in run("show", "3")[1]:
            fails.append("an explicit session wins over the inferred one")
        # A blank project is refused; a non-ASCII one is stored lower case.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "proj.db")
        rc, _, err = run("add", "--project", " ", "--kind", "note", "--ref", "r")
        if rc == 0 or "empty" not in err:
            fails.append(f"an empty project must be refused: {rc} {err!r}")
        out = run("add", "--project", "ÄRGER", "--kind", "note", "--ref", "r")[1]
        if " ärger " not in out:
            fails.append(f"add must lower case a non-ASCII project: {out!r}")

        # Two open sessions in one folder: no guess, and a note says why.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "two.db")
        run("session", "start", "one-1", "--cwd", here)
        run("session", "start", "two-1", "--cwd", here)
        rc, out, err = run("add", "--project", "p", "--kind", "question", "--ref", "q", "--infer-session")
        n = out.split("#", 1)[1].split()[0]
        if rc != 0 or "session" in run("show", n)[1] or "2 open sessions" not in err:
            fails.append(f"two open sessions must store none and say so: {rc} {err!r} {run('show', n)[1]!r}")
        # Created items and notices, added without a schema bump.
        env["LEAN_BRAIN_DB"] = os.environ["LEAN_BRAIN_DB"] = os.path.join(d, "v10.db")
        con = connect()
        if con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != "9" \
                or not {"created", "notices"} <= {r[0] for r in con.execute("SELECT name FROM sqlite_master")}:
            fails.append("a fresh database must stay at schema version 9 and have the created and notices tables")
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if names & {"jobs", "runs"}:
            fails.append(f"a fresh database must have no jobs or runs table: {sorted(names)}")
        if not any(r[1] == "run_id" for r in con.execute("PRAGMA table_info(items)")):
            fails.append("items must keep its run_id column")
        con.close()
        con = connect()  # a second connect is a no-op
        u = "https://github.com/o/r/pull/1"
        c1 = records.add_created(con, "pr", "github", "o/r", "feat/x", u, 1, "t", "s-1", "/w")
        c2 = records.add_created(con, "pr", "github", "o/r", "feat/x", u, 1, "t", "s-1", "/w")
        if c1 != c2 or con.execute("SELECT COUNT(*) FROM created").fetchone()[0] != 1:
            fails.append(f"add_created twice with one url must keep one row: {c1} {c2}")
        records.set_state(con, u, "merged")
        row = con.execute("SELECT state, state_checked_at FROM created WHERE url=?", (u,)).fetchone()
        if row["state"] != "merged" or not row["state_checked_at"]:
            fails.append("set_state must store the state and when it was checked")
        n1 = records.add_notice(con, "h", "text", "s-1", "/w")
        n2 = records.add_notice(con, "h", "text", "s-1", "/w")
        records.add_notice(con, "h", "other", "s-1", "/w")
        if n1 != n2 or con.execute("SELECT COUNT(*) FROM notices").fetchone()[0] != 2:
            fails.append("the same notice inside 10 minutes must be added once")
        con.execute("UPDATE notices SET created_at='2020-01-01T00:00:00Z'")
        con.commit()
        if records.add_notice(con, "h", "text", "s-1", "/w") in (n1, n2):
            fails.append("the same notice after 10 minutes must be added again")
        if records.dismiss_notice(con, 999) or not records.dismiss_notice(con, n1) \
                or not con.execute("SELECT dismissed_at FROM notices WHERE id=?", (n1,)).fetchone()[0]:
            fails.append("dismiss_notice must return False for a missing id and stamp an existing one")
        rc_a, out_a, _ = run("notice", "list")
        rc_b, out_b, _ = run("notice", "list", "--all")
        rc_c, out_c, _ = run("notice", "dismiss", "999")
        rc_d, out_d, _ = run("notice", "dismiss", str(n1 + 1))
        if (rc_a, rc_b, rc_c, rc_d) != (0, 0, 1, 0) or len(out_b.splitlines()) != len(out_a.splitlines()) + 1 \
                or out_d != f"dismissed {n1 + 1}" or f"dismissed" not in out_b:
            fails.append(f"notice list hides dismissed ones, --all shows them, dismiss reports a missing id: {out_a!r} {out_b!r} {out_c!r} {out_d!r}")
        con.close()
        # Old files keep their jobs and runs tables, row for row. Fixtures stand in for the old schema.
        old_tables = ("CREATE TABLE runs (id INTEGER PRIMARY KEY, project TEXT NOT NULL, test_project TEXT, host TEXT)",
                      "CREATE TABLE jobs (id INTEGER PRIMARY KEY, cycle TEXT, project TEXT NOT NULL, repo_dir TEXT,"
                      " test_project TEXT, tier TEXT, enqueued_by TEXT, enqueued_at TEXT, status TEXT)")

        def old_file(name, version):
            path = os.path.join(d, name)
            c = sq.connect(path)
            c.row_factory = sq.Row
            c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for m in MIGRATIONS[:version]:
                m(c)
            for q in old_tables:
                c.execute(q)
            c.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(version),))
            c.execute("INSERT INTO items (project, kind, ref, title, status, occurrences, first_seen, last_seen)"
                      " VALUES ('p','note','r','r','open',1,'2026-10-18T00:00:00Z','2026-10-18T00:00:00Z')")
            c.execute("INSERT INTO runs (project, test_project, host) VALUES ('p','T','dev')")
            c.execute("INSERT INTO jobs (cycle, project, repo_dir, test_project, tier, enqueued_by, enqueued_at, status)"
                      " VALUES ('c','p','/r','T','full','me','2026-10-18T00:00:00Z','done')")
            c.commit()
            c.close()
            return path

        for name, version in (("v9.db", 9), ("v4.db", 4)):
            path = old_file(name, version)
            env["LEAN_BRAIN_DB"] = os.environ["LEAN_BRAIN_DB"] = path
            con = connect()
            con.close()
            jobs = [tuple(r) for r in connect_rows(path, "SELECT cycle, project, repo_dir, test_project, tier,"
                                                         " enqueued_by, enqueued_at, status FROM jobs")]
            runs = [tuple(r) for r in connect_rows(path, "SELECT project, test_project, host FROM runs")]
            ver = connect_rows(path, "SELECT value FROM meta WHERE key='schema_version'")[0][0]
            n_items = connect_rows(path, "SELECT COUNT(*) FROM items")[0][0]
            n_created = connect_rows(path, "SELECT COUNT(*) FROM created")[0][0]
            if ver != "9" or n_items != 1 or n_created != 0 \
                    or jobs != [("c", "p", "/r", "T", "full", "me", "2026-10-18T00:00:00Z", "done")] \
                    or runs != [("p", "T", "dev")]:
                fails.append(f"a version {version} database must reach 9 and keep its items, jobs and runs: "
                             f"{ver} {n_items} {jobs} {runs}")

        # The moved commands forward to test-phase. A fake one in a temp dir stands in for it.
        fake_dir = os.path.join(d, "fakebin")
        os.makedirs(fake_dir)
        fake = os.path.join(fake_dir, "test-phase")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho \"fake $*\"\nexit 7\n")
        os.chmod(fake, 0o755)
        empty_home = os.path.join(d, "emptyhome")
        os.makedirs(empty_home)
        empty_path = os.path.join(d, "emptybin")
        os.makedirs(empty_path)
        env["LEAN_BRAIN_DB"] = os.path.join(d, "fwd.db")
        saved_env = dict(env)
        env["HOME"] = empty_home
        env["PATH"] = fake_dir + os.pathsep + "/usr/bin:/bin"
        rc, out, err = run("queue", "list", "--status", "queued")
        if rc != 7 or out != "fake queue list --status queued" \
                or 'brain: "brain queue" moved to "test-phase queue"' not in err:
            fails.append(f"brain queue must run test-phase with the same arguments: {rc} {out!r} {err!r}")
        env["PATH"] = empty_path
        rc, out, err = run("queue", "list", "--status", "queued")
        if rc != 2 or "test-phase was not found" not in err or out:
            fails.append(f"with no test-phase, brain queue must exit 2 and say so: {rc} {out!r} {err!r}")
        env.clear()
        env.update(saved_env)
        # The calls lean-agent-tools makes through the brain command.
        env["LEAN_BRAIN_DB"] = os.path.join(d, "contract.db")
        rc, out, _ = run("--version")
        if (rc, out) != (0, "brain 1.0.0"):
            fails.append(f"--version must print brain 1.0.0: {rc} {out!r}")
        rc, out, _ = run("list", "--json")
        if (rc, out) != (0, "[]"):
            fails.append(f"list --json on an empty database must print []: {rc} {out!r}")
        run("add", "--project", "p", "--kind", "test-fail", "--ref", "A", "--tag", "zeta", "--tag", "alpha")
        run("add", "--project", "p", "--kind", "test-fail", "--ref", "B")
        run("add", "--project", "p", "--kind", "note", "--ref", "C")
        rc, out, _ = run("list", "--json", "--kind", "test-fail")
        want_keys = sorted("id kind status project repo branch cycle run_id ref title tags first_seen last_seen".split())
        try:
            rows = json.loads(out)
        except ValueError:
            rows = []
        if rc != 0 or len(rows) != 2 or any(sorted(r) != want_keys for r in rows) or rows[0]["tags"] != ["alpha", "zeta"] \
                or rows[1]["tags"] != []:
            fails.append(f"list --json must give two objects with the 13 keys and sorted tags: {out!r}")
        text_ids = [l.split()[0].lstrip("#") for l in run("list", "--kind", "test-fail")[1].splitlines()]
        if [str(r["id"]) for r in rows] != text_ids or len(json.loads(run("list", "--json")[1])) != 3:
            fails.append("list --json must keep the rows and order of the text list, and every status without a filter")
        rc, a, _ = run("notice", "add", "--hook", "h1", stdin="something happened\n")
        rc2, b, _ = run("notice", "add", "--hook", "h1", stdin="something happened\n")
        if rc != 0 or rc2 != 0 or not a.isdigit() or a != b:
            fails.append(f"notice add twice with one hook and text must print one id: {a!r} {b!r}")
        rc, _, _ = run("notice", "add", "--hook", "h1", stdin="")
        if rc != 2:
            fails.append(f"notice add with empty stdin must exit 2: {rc}")
        args = ("created", "add", "--kind", "pr", "--forge", "github", "--url", "https://example.test/pr/1",
                "--repo", "o/r", "--number", "1", "--title", "t")
        rc, a, _ = run(*args)
        rc2, b, _ = run(*args)
        if rc != 0 or rc2 != 0 or not a.isdigit() or a != b:
            fails.append(f"created add twice with one url must print one id: {a!r} {b!r}")
        env["LEAN_BRAIN_DB"] = os.path.join(d, "infer-json.db")
        here = os.path.realpath(os.getcwd())
        rc, out, err = run("session", "infer", "--json")
        if (rc, json.loads(out or "{}")) != (0, {"id": "", "cwd": ""}) or err:
            fails.append(f"session infer with no session must print empty ids: {rc} {out!r} {err!r}")
        run("session", "start", "one-1", "--cwd", here)
        rc, out, err = run("session", "infer", "--json")
        if json.loads(out or "{}") != {"id": "one-1", "cwd": here} or err:
            fails.append(f"session infer must return the one open session of the folder: {out!r} {err!r}")
        run("session", "start", "two-1", "--cwd", here)
        rc, out, err = run("session", "infer", "--json")
        if json.loads(out or "{}") != {"id": "", "cwd": ""} or len(err.splitlines()) != 1:
            fails.append(f"session infer with two open sessions must print empty ids and one stderr line: {out!r} {err!r}")
        serve_tests(fails, me, d)
    if fails:
        print("self-test failed:\n  " + "\n  ".join(fails))
        return 1
    print("self-test passed")
    return 0
