"""A local list of what went wrong and is not fixed yet, shared by every session on the machine.

  brain add --project P --kind K --ref R [--repo R] [--branch B] [--title T] [--body B | --body-file F]
            [--severity S] [--phase P] [--source S] [--model M] [--cycle C] [--commit H] [--link L]
            [--run-id N] [--tag T ...] [--session ID] [--session-cwd DIR] [--infer-session]
  brain list [--open] [--status S] [--project P] [--kind K] [--tag T] [--cycle C]
  brain show ID
  brain close ID (--resolution R | --wontfix) [--note N] [--commit H] [--tag T ...]
  brain reopen ID
  brain queue ..., run-add ..., flaky-clear ...   moved to test-phase (tools/test-phase queue|run-add|flaky-clear);
            brain forwards the arguments to it, prints the moved line on stderr, and exits 2 when test-phase is not found
  brain activity add --source S --action A [--ts T] [--project P] [--target T] [--ok 0|1] [--duration-ms N]
            [--error E] [--host H]
  brain activity list [--source S] [--project P] [--since 24h] [--failed] [--limit N]      newest first
  brain activity import-baryovm [--file PATH]   reads BaryoVM's activity.jsonl from where the last import
            stopped, so a rerun adds nothing; a file that shrank is read from the start
  brain created list [--open] [--limit N]     pull requests, issues, releases and work items agents created, newest first
  brain notice list [--all] [--limit N]       hook notes kept as notices, newest first; --all adds dismissed ones
  brain notice dismiss ID
  brain resume [--project P] [--max N]
  brain digest [--project P | --all] [--quiet] [--recover]
  brain stats [--json]
  brain export [--format md|json] [--project P] [--status S]
  brain session start|end|status ID [--cwd DIR]
  brain session infer --json      {"id": "...", "cwd": "..."} of the one open session started in this folder, else both empty
  brain list --json               the same rows as list, as a JSON array
  brain notice add --hook H [--session S] [--cwd C]      the text on stdin; prints the id (same hook and text within 10 minutes is one notice)
  brain created add --kind K --forge F --url U [--repo R --branch B --number N --title T --session S --cwd C]      prints the id
  brain --version
  brain answers --session ID
  brain serve [--port N] [--roots A,B] [--idle-exit MIN] [--install always|session|manual | --uninstall]
            the brain page at http://leanagent.localhost:PORT (also http://127.0.0.1:PORT), port 8770 or
            $LEAN_SERVE_PORT. A second serve on a port where the page already answers prints its address and
            exits 0. --idle-exit stops it after MIN minutes with no open session. --install always starts it
            at login (launchd, systemd user unit or Windows Startup folder; no admin rights); the other two
            modes remove that and leave the start to the plugin's serve_mode setting. --uninstall removes it
  brain --self-test

The file is ${LEAN_BRAIN_DB:-~/.local/share/lean/brain.db}, one sqlite database with a schema version
(table meta) that is migrated forward on open. Nothing leaves the machine. Output is plain text, one
line per item. --body-file - reads the body from stdin.

Kinds: test-fail, flaky, minor, question, ticket, security, note, failure, risk, ruling, bug,
review-finding, isolation, and three for surviving a context compaction: pending (the next step),
blocker (waiting on the user, or a denied action that must not be worked around) and agent (a running
subagent: id, task and worktree in the title or body; closed when it reports back). Aliases: todo and
task mean pending, open-question means question, finding means review-finding, running and
running-agent mean agent. Any other kind is refused. Project names are stored lower case. close needs
--resolution, or --wontfix. An open agent item not updated for 24 hours shows as stale in stats.
Never store secrets, tokens or passwords.
resume prints the open items of one project as a short digest: blockers, agents,
pending, questions, then the rest, newest first, titles only, at most --max lines (default 40). The project defaults to the
git toplevel's name in the current directory. It prints nothing and exits 0 when there is no database
or nothing open. The SessionStart hook runs it on startup and after a compaction.
digest prints under 2 KB: open items by project and kind (project names match without regard to case),
open blockers and questions one line each, and agent items open with no update for 24 hours.
It covers the current project, or every project with --all. The brain_digest MCP tool runs it.
Status: open, closed, wontfix. Resolution: fixed, flaky, flaky-cleared, not-a-bug, duplicate, answered, filed.
Severity: critical, important, minor, info. Phase: code, test, fix, review.
Source: test-phase, agent, review, user. Model: haiku, sonnet, opus, or empty.
add stores the Claude session that filed the item and its folder (--session, --session-cwd, defaulting
to $LEAN_SESSION_ID and $LEAN_SESSION_CWD, which the session-env hook sets), so an answer can resume it.
session records whether a session is open or ended (the session-env hook calls it), so an answer
never starts a second copy of a live session. answers prints the answered questions a session filed that
it has not had yet and marks them delivered; close --delivered marks one delivered at once.

Adding an open item with the same project, kind and ref does not make a new row: it raises
occurrences and moves last_seen, so a failure seen in ten runs is one line.

What goes where. Brain holds anything with a status that changes: test failures, flaky classes,
deferred minors, open questions for the user, unfiled or filed tickets, security findings. Markdown
ledgers keep rulings (decisions, with the cost if wrong), task complete lines and user decisions.
Memory files keep standing preferences. Run logs are scratch and can expire once their failures are in
brain. A ledger points to open items with a line like "open items: brain list --project X --status open".
"""
import argparse
import errno
import socket
import os
import sys
import threading
import time
import urllib.request

from .version import VERSION
from .store import norm_project, MODEL, PHASE, RESOLUTION, SEVERITY, SOURCE, STATUS
from .items import cmd_add, cmd_answers, cmd_close, cmd_list, cmd_reopen, cmd_session, cmd_show
from .activity import cmd_activity
from .records import cmd_created, cmd_notice
from .forward import MOVED, forward
from .report import cmd_digest, cmd_export, cmd_resume, cmd_stats


def lower(s):
    name = norm_project(s)
    if not name:
        raise argparse.ArgumentTypeError("a project name cannot be empty")
    return name


def parser():
    p = argparse.ArgumentParser(prog="brain", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"brain {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add")
    s.add_argument("--project", required=True, type=lower)
    s.add_argument("--repo", default="")
    s.add_argument("--branch", default="")
    s.add_argument("--kind", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--title", default="")
    s.add_argument("--body", "--detail", default="")
    s.add_argument("--body-file")
    s.add_argument("--severity", default="", choices=SEVERITY + ("",))
    s.add_argument("--phase", default="", choices=PHASE + ("",))
    s.add_argument("--source", default="", choices=SOURCE + ("",))
    s.add_argument("--model", default="", choices=MODEL)
    s.add_argument("--cycle", default="")
    s.add_argument("--commit", default="")
    s.add_argument("--link", default="")
    s.add_argument("--run-id", type=int)
    s.add_argument("--tag", action="append")
    s.add_argument("--session", default=os.environ.get("LEAN_SESSION_ID", ""),
                   help="the Claude session that filed it (default $LEAN_SESSION_ID)")
    s.add_argument("--session-cwd", default=os.environ.get("LEAN_SESSION_CWD", ""),
                   help="that session's folder (default $LEAN_SESSION_CWD)")
    s.add_argument("--infer-session", action="store_true",
                   help="with no session, take the open one started in this folder")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("list")
    s.add_argument("--open", action="store_true")
    s.add_argument("--status", choices=STATUS)
    s.add_argument("--project", type=lower)
    s.add_argument("--kind")
    s.add_argument("--tag")
    s.add_argument("--cycle")
    s.add_argument("--json", action="store_true", help="a JSON array of objects, same rows and order")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("show")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("close")
    s.add_argument("id", type=int)
    s.add_argument("--wontfix", action="store_true")
    s.add_argument("--resolution", default="", choices=RESOLUTION + ("",))
    s.add_argument("--note", default="")
    s.add_argument("--commit", default="")
    s.add_argument("--tag", action="append")
    s.add_argument("--delivered", action="store_true", help="the answer already reached the asking session")
    s.set_defaults(fn=cmd_close)
    s = sub.add_parser("session")
    s.add_argument("action", choices=("start", "end", "status", "infer"))
    s.add_argument("id", nargs="?", default="")
    s.add_argument("--cwd", default="")
    s.add_argument("--json", action="store_true", help="with infer: print {\"id\": ..., \"cwd\": ...}")
    s.set_defaults(fn=cmd_session)
    s = sub.add_parser("answers")
    s.add_argument("--session", required=True)
    s.set_defaults(fn=cmd_answers)
    s = sub.add_parser("reopen")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_reopen)
    s = sub.add_parser("activity")
    acs = s.add_subparsers(dest="acmd", required=True)
    q = acs.add_parser("add")
    q.add_argument("--source", required=True)
    q.add_argument("--action", required=True)
    q.add_argument("--ts", default="")
    q.add_argument("--project", default="")
    q.add_argument("--target", default="")
    q.add_argument("--ok", type=int, choices=(0, 1), default=1)
    q.add_argument("--duration-ms", type=int)
    q.add_argument("--error", default="")
    q.add_argument("--host", default=socket.gethostname())
    q = acs.add_parser("list")
    q.add_argument("--source")
    q.add_argument("--project")
    q.add_argument("--since", help="like 24h, 7d, 30m")
    q.add_argument("--failed", action="store_true")
    q.add_argument("--limit", type=int, default=50)
    q = acs.add_parser("import-baryovm")
    q.add_argument("--file", help="default $BARYOVM_HOME/activity.jsonl, or ~/.baryovm/activity.jsonl")
    s.set_defaults(fn=cmd_activity)
    s = sub.add_parser("created")
    cs = s.add_subparsers(dest="ccmd", required=True)
    q = cs.add_parser("add")
    q.add_argument("--kind", required=True, choices=("pr", "issue", "release", "workitem"))
    q.add_argument("--forge", required=True, choices=("github", "azure"))
    q.add_argument("--url", required=True)
    q.add_argument("--repo", default="")
    q.add_argument("--branch", default="")
    q.add_argument("--number", type=int)
    q.add_argument("--title", default="")
    q.add_argument("--session", default="")
    q.add_argument("--cwd", default="")
    q = cs.add_parser("list")
    q.add_argument("--open", action="store_true", help="only items not known to be closed or merged")
    q.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_created)
    s = sub.add_parser("notice")
    ns = s.add_subparsers(dest="ncmd", required=True)
    q = ns.add_parser("add")
    q.add_argument("--hook", required=True)
    q.add_argument("--session", default="")
    q.add_argument("--cwd", default="")
    q = ns.add_parser("list")
    q.add_argument("--all", action="store_true", help="include dismissed notices")
    q.add_argument("--limit", type=int, default=50)
    q = ns.add_parser("dismiss")
    q.add_argument("id", type=int)
    s.set_defaults(fn=cmd_notice)
    s = sub.add_parser("resume")
    s.add_argument("--project", type=lower)
    s.add_argument("--max", type=int, default=40)
    s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("digest")
    s.add_argument("--project", help="default: the git toplevel's name in the current directory")
    s.add_argument("--all", action="store_true", help="every project")
    s.add_argument("--quiet", action="store_true", help="print nothing when nothing is open")
    s.add_argument("--recover", action="store_true",
                   help="after the digest, every open agent and pending item, 4 KB at most")
    s.set_defaults(fn=cmd_digest)
    s = sub.add_parser("stats")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("export")
    s.add_argument("--format", choices=("md", "json"), default="md")
    s.add_argument("--project", type=lower)
    s.add_argument("--status", choices=STATUS)
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    s.add_argument("--roots", default="", help="comma-separated folders to look for worktrees in")
    s.add_argument("--idle-exit", type=int, metavar="MIN")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--install", choices=("always", "session", "manual"))
    g.add_argument("--uninstall", action="store_true")
    s.set_defaults(fn=cmd_serve)
    return p


def healthz(port, timeout=1.0):
    """True when the brain page answers on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            return r.read(64).startswith(b"lean-brain")
    except (OSError, ValueError):
        return False


def idle_watch(db_file, minutes, state, now=None):
    """True when no session has been open for `minutes`. An unreadable database counts as a session open.
    A session row older than 12 hours does not count: a crashed session never writes ended_at."""
    import sqlite3
    now = time.time() if now is None else now
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2)
        try:
            cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 12 * 3600))
            open_now = con.execute("SELECT 1 FROM sessions WHERE ended_at='' AND started_at>? LIMIT 1",
                                   (cutoff,)).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        open_now = True
    if open_now or "last" not in state:
        state["last"] = now
    return now - state["last"] >= minutes * 60


def cmd_serve(a):
    from .web import install
    plat = install.platform_name()
    brain_path = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "tools", "brain"))
    try:
        port = a.port or int(os.environ.get("LEAN_SERVE_PORT") or 8770)
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        print("brain serve: the port must be a number from 1 to 65535", file=sys.stderr)
        return 2
    roots = a.roots or os.environ.get("CLAUDE_PLUGIN_OPTION_WORKTREE_ROOTS", "")
    if a.uninstall:
        print("removed the login unit" if install.uninstall(plat) else "no login unit installed")
        return 0
    if a.install:
        if a.install != "always":
            gone = install.uninstall(plat)
            print(("removed the login unit; " if gone else "") + f"mode {a.install}: set serve_mode in the plugin settings")
            return 0
        path, started = install.install(plat, sys.executable, brain_path, port, roots)
        print(f"wrote {path}" + ("" if started else " (could not start it now; it starts at the next login)"))
        return 0
    urls = f"http://leanagent.localhost:{port}  (also http://127.0.0.1:{port})"
    if healthz(port):
        print(urls)
        return 0
    from .web import server
    from . import store
    import secrets
    try:
        srv = server.make_server(port, secrets.token_urlsafe(32), roots=[r.strip() for r in roots.split(",") if r.strip()])
    except OSError as ex:
        print(f"port {port} is in use" if ex.errno == errno.EADDRINUSE else f"brain serve: {ex}", file=sys.stderr)
        return 1
    print(urls, flush=True)
    if a.idle_exit:
        db_file, state = store.db_path(), {}

        def watch():
            while True:
                try:
                    if idle_watch(db_file, a.idle_exit, state):
                        break
                except Exception:  # whatever goes wrong here must not stop the page
                    pass
                time.sleep(60)
            srv.shutdown()
        threading.Thread(target=watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv):
    if argv and argv[0] in MOVED:
        return forward(argv)
    args = parser().parse_args(argv)
    return args.fn(args)
