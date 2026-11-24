import json
import re
import sys
import time

import os

from .store import KIND, RESOLUTION, canon_kind, connect, now


def tags_of(con, item_id):
    return [r["tag"] for r in con.execute("SELECT tag FROM tags WHERE item_id=? ORDER BY tag", (item_id,))]


def line(con, r):
    where = r["project"] + ("@" + r["branch"] if r["branch"] else "")
    s = f"#{r['id']} {r['status']} {r['kind']} {where} {r['ref']}"
    if r["severity"]:
        s += f" {r['severity']}"
    s += f" occ={r['occurrences']} last={r['last_seen']}"
    t = tags_of(con, r["id"])
    return s + (" tags=" + ",".join(t) if t else "")


def add_tags(con, item_id, tags):
    for t in tags or []:
        con.execute("INSERT OR IGNORE INTO tags (item_id, tag) VALUES (?,?)", (item_id, t))


def get(con, ident):
    r = con.execute("SELECT * FROM items WHERE id=?", (ident,)).fetchone()
    if not r:
        sys.exit(f"brain: no item #{ident}")
    return r


def body_of(a):
    if a.body_file:
        return sys.stdin.read() if a.body_file == "-" else open(a.body_file).read()
    return a.body or ""


def session_ok(ident):
    return bool(re.fullmatch(r"[A-Za-z0-9-]{1,100}", ident))


def infer_session(con):
    """The open session started in this folder, as (id, cwd). ("", "") when there is none or more than one."""
    here = os.path.realpath(os.getcwd())
    rows = con.execute("SELECT id, cwd FROM sessions WHERE ended_at='' AND cwd=?", (here,)).fetchall()
    if len(rows) > 1:
        print(f"brain: {len(rows)} open sessions in {here}, so no session was stored", file=sys.stderr)
    return (rows[0]["id"], rows[0]["cwd"]) if len(rows) == 1 else ("", "")


def kind_or_exit(kind):
    k = canon_kind(kind)
    if not k:
        sys.exit(f"brain: unknown kind {kind!r}; use one of: {', '.join(KIND)} "
                 "(aliases: todo, task, open-question, finding, running, running-agent)")
    return k


def cmd_add(a):
    if a.session and not session_ok(a.session):
        sys.exit("brain: --session takes letters, digits and dashes only")
    a.kind = kind_or_exit(a.kind)
    con = connect()
    if not a.session and a.infer_session:
        a.session, a.session_cwd = infer_session(con)
    body = body_of(a)
    t = now()
    with con:
        row = con.execute("SELECT * FROM items WHERE status='open' AND project=? AND kind=? AND ref=?",
                          (a.project, a.kind, a.ref)).fetchone()
        if row:
            sets = {"last_seen": t}
            for col, val in (("branch", a.branch), ("body", body), ("repo", a.repo), ("cycle", a.cycle),
                             ("severity", a.severity), ("link", a.link), ("run_id", a.run_id),
                             ("session_id", a.session), ("session_cwd", a.session_cwd if a.session else "")):
                if val not in ("", None):
                    sets[col] = val
            con.execute("UPDATE items SET occurrences=occurrences+1, " + ", ".join(f"{k}=?" for k in sets)
                        + " WHERE id=?", list(sets.values()) + [row["id"]])
            item_id, verb = row["id"], "seen again"
        else:
            cur = con.execute(
                "INSERT INTO items (project, repo, branch, kind, ref, title, body, severity, phase, source, model,"
                " cycle, commit_hash, link, run_id, first_seen, last_seen, session_id, session_cwd)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a.project, a.repo, a.branch, a.kind, a.ref, a.title or a.ref, body, a.severity, a.phase,
                 a.source, a.model, a.cycle, a.commit, a.link, a.run_id, t, t, a.session,
                 a.session_cwd if a.session else ""))
            item_id, verb = cur.lastrowid, "added"
        add_tags(con, item_id, a.tag)
        print(f"{verb}: " + line(con, get(con, item_id)))
    return 0


JSON_COLUMNS = ("id", "kind", "status", "project", "repo", "branch", "cycle", "run_id", "ref", "title",
                "first_seen", "last_seen")


def cmd_list(a):
    q, args = "SELECT DISTINCT items.* FROM items", []
    if a.tag:
        q += " JOIN tags ON tags.item_id = items.id AND tags.tag = ?"
        args.append(a.tag)
    q += " WHERE 1=1"
    status = "open" if a.open else a.status
    kind = canon_kind(a.kind) or a.kind
    for col, val in (("status", status), ("project", a.project), ("kind", kind), ("cycle", a.cycle)):
        if val:
            q += f" AND items.{col}=?"
            args.append(val)
    con = connect()
    rows = con.execute(q + " ORDER BY items.id", args).fetchall()
    if a.json:
        print(json.dumps([dict({k: r[k] for k in JSON_COLUMNS}, tags=tags_of(con, r["id"])) for r in rows]))
        return 0
    for r in rows:
        print(line(con, r))
    return 0


def cmd_show(a):
    con = connect()
    r = get(con, a.id)
    print(line(con, r))
    print(f"title: {r['title']}")
    print(f"first seen: {r['first_seen']}")
    for col in ("repo", "resolution", "phase", "source", "model", "cycle", "commit_hash", "link", "run_id", "closed_at",
                "session_id", "session_cwd"):
        if r[col] not in ("", None):
            label = "session" if col == "session_id" else col.replace("_hash", "").replace("_", " ")
            print(f"{label}: {r[col]}")
    if r["body"]:
        print("body: " + r["body"])
    return 0


def close_item(con, item_id, resolution, note="", wontfix=False, delivered=False, commit=""):
    """Close an open item in one statement. False when it is not open (already closed, or gone)."""
    t = now()
    cur = con.execute("UPDATE items SET status=?, resolution=?, closed_at=?, body=body || ?, answer_delivered_at=?,"
                      " commit_hash=COALESCE(NULLIF(?, ''), commit_hash) WHERE id=? AND status='open'",
                      ("wontfix" if wontfix else "closed", resolution, t, ("\n\nclosed: " + note) if note else "",
                       t if delivered else "", commit or "", item_id))
    return cur.rowcount > 0


def cmd_close(a):
    con = connect()
    with con:
        r = get(con, a.id)
        if r["status"] != "open":
            sys.exit(f"brain: #{a.id} is already {r['status']}")
        if not a.resolution and not a.wontfix:
            sys.exit(f"brain: close needs --resolution ({', '.join(RESOLUTION)}), or --wontfix")
        if not close_item(con, a.id, a.resolution, a.note, wontfix=a.wontfix, delivered=a.delivered, commit=a.commit):
            sys.exit(f"brain: #{a.id} is already {get(con, a.id)['status']}")
        add_tags(con, a.id, a.tag)
        print("closed: " + line(con, get(con, a.id)))
    return 0


def session_start(con, sid, cwd):
    with con:
        con.execute("INSERT INTO sessions (id, cwd, started_at) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE"
                    " SET cwd=excluded.cwd, started_at=excluded.started_at, ended_at=''", (sid, cwd, now()))


def session_end(con, sid):
    with con:
        con.execute("UPDATE sessions SET ended_at=? WHERE id=?", (now(), sid))


def take_answers(con, sid):
    """The lines for this session's answered questions that it has not had yet, now marked delivered."""
    lines = []
    with con:
        rows = con.execute("SELECT * FROM items WHERE session_id=? AND kind='question' AND resolution='answered'"
                           " AND answer_delivered_at='' ORDER BY id", (sid,)).fetchall()
        for r in rows:
            answer = r["body"].rsplit("\n\nclosed: ", 1)[1] if "\n\nclosed: " in r["body"] else ""
            lines.append(f"#{r['id']} {r['title']}: {answer}")
            con.execute("UPDATE items SET answer_delivered_at=? WHERE id=?", (now(), r["id"]))
    return lines


def cmd_session(a):
    if a.action == "infer":
        sid, cwd = infer_session(connect())
        print(json.dumps({"id": sid, "cwd": cwd}))
        return 0
    if not a.id:
        sys.exit("brain: session start, end and status need an id")
    if not session_ok(a.id):
        sys.exit("brain: a session id takes letters, digits and dashes only")
    con = connect()
    if a.action == "start":
        session_start(con, a.id, a.cwd)
    elif a.action == "end":
        session_end(con, a.id)
    else:
        with con:
            r = con.execute("SELECT * FROM sessions WHERE id=?", (a.id,)).fetchone()
            print("unknown" if not r else f"{'ended' if r['ended_at'] else 'open'} {r['cwd']}")
    return 0


def cmd_answers(a):
    """Print the answers to this session's questions that it has not had yet, and mark them delivered."""
    for text in take_answers(connect(), a.session):
        print(text)
    return 0


def cmd_reopen(a):
    con = connect()
    with con:
        r = get(con, a.id)
        if r["status"] == "open":
            sys.exit(f"brain: #{a.id} is already open")
        dupe = con.execute("SELECT id FROM items WHERE status='open' AND project=? AND kind=? AND ref=?",
                           (r["project"], r["kind"], r["ref"])).fetchone()
        if dupe:
            sys.exit(f"brain: #{dupe['id']} is the same item and still open")
        con.execute("UPDATE items SET status='open', resolution='', closed_at=NULL WHERE id=?", (a.id,))
        print("reopened: " + line(con, get(con, a.id)))
    return 0
