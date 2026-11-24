#!/usr/bin/env python3
"""Tie brain items to the Claude session that filed them, so an answer can go back to that session.

SessionStart: writes LEAN_SESSION_ID and LEAN_SESSION_CWD to $CLAUDE_ENV_FILE, so `brain add` in this
session stores them, appends brain's tools folder to PATH there, and records the session as open in brain.
SessionEnd: records it as ended. UserPromptSubmit: hands the session the answers to its questions that it
has not had yet. The other plugin may write the same session lines; each hook replaces its own earlier lines.
Never fails the session. LEAN_SESSION_ENV=off turns off the session lines, the record and the answers, not
the PATH line. LEAN_PROFILE=off turns everything off.

  brain-session.py --self-test
"""
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
LIB = os.path.join(HERE, "..", "lib")
sys.path.insert(0, LIB)
from brain import hooks as bh  # noqa: E402

HOOK = {
    "events": [
        {"event": "SessionStart", "group": 5, "order": 0},
        {"event": "SessionEnd", "group": 1, "order": 0},
        {"event": "UserPromptSubmit", "group": 0, "order": 0},
    ],
    "timeout": 10,
    "mode": "process",
    "pack": "core",
    "guard": False,
    "default": {"strict": "note", "standard": "note"},
}

BRAIN = os.path.join(HERE, "..", "tools", "brain")
SESSION_KEYS = ("export LEAN_SESSION_ID=", "export LEAN_SESSION_CWD=")


def ours(session):
    """Which env lines this hook replaces: its own PATH line, and the session lines when it writes them."""
    def match(line):
        return bh.ours_path(line) or (session and line.startswith(SESSION_KEYS))
    return match


def env_lines(sid, cwd, session):
    lines = []
    if session:
        lines.append(f"export LEAN_SESSION_ID={shlex.quote(sid)}")
        if cwd:
            lines.append(f"export LEAN_SESSION_CWD={shlex.quote(cwd)}")
    return lines + [bh.path_line()]


def main():
    if bh.off():
        return 0
    from brain import items, store
    event = bh.read_event()
    name = event.get("hook_event_name")
    sid = event.get("session_id")
    sid = sid if isinstance(sid, str) and items.session_ok(sid) else None
    cwd = str(event.get("cwd") or "")
    if any(c in cwd for c in "\r\n\x00"):  # a line break would split the env line on the next write
        cwd = ""
    session_off = (os.environ.get("LEAN_SESSION_ENV") or "").lower() == "off"
    if name == "SessionStart":
        session = bool(sid) and not session_off
        bh.append_env(env_lines(sid, cwd, session), ours(session))
    if session_off or not sid or name not in ("SessionStart", "SessionEnd", "UserPromptSubmit"):
        return 0
    try:
        con = store.connect(migrate=True, timeout=3)
        try:
            if name == "SessionStart":
                items.session_start(con, sid, cwd)
            elif name == "SessionEnd":
                items.session_end(con, sid)
            else:
                answers = items.take_answers(con, sid)
                if answers:
                    bh.context("UserPromptSubmit", "Answers to questions you filed in brain (already closed as "
                               "answered):\n" + "\n".join(answers))
        finally:
            con.close()
    except sqlite3.Error:  # a locked database: the session goes on without the record
        return 0
    return 0


def self_test():
    import json
    fails = []
    me = os.path.realpath(__file__)
    with tempfile.TemporaryDirectory() as d:
        home = os.path.join(d, "home")
        os.mkdir(home)
        env_file = os.path.join(d, "env")
        env = dict(os.environ, LEAN_BRAIN_DB=os.path.join(d, "brain.db"), LEAN_TESTS_DB=os.path.join(d, "tests.db"),
                   CLAUDE_ENV_FILE=env_file, HOME=home)
        for k in ("LEAN_SESSION_ENV", "LEAN_SESSION_ID", "LEAN_SESSION_CWD", "LEAN_PROFILE"):
            env.pop(k, None)
        cwd = os.path.join(d, "it's a dir")
        os.mkdir(cwd)
        other_tools = os.path.join(d, "plugins", "cache", "m", "lean-agent-tools", "2.0.0", "tools")
        other_path = f'export PATH="${{PATH:+$PATH:}}"{shlex.quote(other_tools)}'
        mine = bh.path_line()

        def hook(name, sid="abc-123", raw=None, **extra):
            event = raw if raw is not None else json.dumps({"hook_event_name": name, "session_id": sid, "cwd": cwd})
            r = subprocess.run([sys.executable, me], input=event, env=dict(env, **extra), capture_output=True,
                               text=True)
            return r.returncode, r.stdout, r.stderr

        def brain_out(*args):
            return subprocess.run([sys.executable, BRAIN, *args], env=env, capture_output=True,
                                  text=True).stdout.strip()

        def seed(sid="abc-123"):
            return [f"export LEAN_SESSION_ID={sid}", f"export LEAN_SESSION_CWD={shlex.quote(cwd)}", other_path]

        def other_writer(path, sid="abc-123"):
            """What the other plugin's hook does to the file: replace every session line and only its own PATH line."""
            with open(path) as fh:
                lines = fh.read().splitlines()
            kept = [ln for ln in lines if not ln.startswith(SESSION_KEYS) and ln != other_path]
            with open(path, "w") as fh:
                fh.write("".join(ln + "\n" for ln in kept + seed(sid)))

        def shape(path, sid="abc-123"):
            lines = open(path).read().splitlines()
            ids = [ln for ln in lines if ln.startswith("export LEAN_SESSION_ID=")]
            cwds = [ln for ln in lines if ln.startswith("export LEAN_SESSION_CWD=")]
            ok = (ids == [f"export LEAN_SESSION_ID={sid}"] and cwds == [f"export LEAN_SESSION_CWD={shlex.quote(cwd)}"]
                  and lines.count(mine) == 1 and lines.count(other_path) == 1
                  and len([ln for ln in lines if ln.startswith("export PATH=")]) == 2)
            return ok, lines

        # both orders, and a resume after each
        with open(env_file, "w") as fh:
            fh.write("".join(ln + "\n" for ln in seed()))
        rc, out, err = hook("SessionStart")
        if rc != 0 or out or err:
            fails.append(f"start must be quiet: {rc} {out!r} {err!r}")
        ok, lines = shape(env_file)
        if not ok:
            fails.append(f"after the other writer, each session line once and one PATH line per folder: {lines!r}")
        if lines and lines[-1] != mine:
            fails.append(f"brain's PATH line must come last: {lines!r}")
        hook("SessionStart", raw=json.dumps({"hook_event_name": "SessionStart", "session_id": "abc-123", "cwd": cwd,
                                             "source": "compact"}))
        ok, lines = shape(env_file)
        if not ok:
            fails.append(f"a resume after the other writer must change nothing: {lines!r}")
        sourced = subprocess.run(["bash", "-c", f". {shlex.quote(env_file)}; printf '%s|%s' \"$LEAN_SESSION_ID\" "
                                  "\"$LEAN_SESSION_CWD\""], capture_output=True, text=True).stdout
        if sourced != f"abc-123|{cwd}":
            fails.append(f"env file must export the id and a quoted cwd: {sourced!r}")
        shown = subprocess.run(["bash", "-c", f"PATH=/usr/bin:/bin; . {shlex.quote(env_file)}; command -v brain"],
                               capture_output=True, text=True).stdout.strip()
        if shown != os.path.join(bh.TOOLS, "brain"):
            fails.append(f"a shell sourcing the env file must find brain in brain's tools folder: {shown!r}")
        order_b = os.path.join(d, "order-b-env")
        env["CLAUDE_ENV_FILE"] = order_b
        hook("SessionStart")
        other_writer(order_b)
        ok, lines = shape(order_b)
        if not ok:
            fails.append(f"brain first, then the other writer: {lines!r}")
        hook("SessionStart")
        ok, lines = shape(order_b)
        if not ok or lines[-1] != mine:
            fails.append(f"a resume after both must leave each line once, brain's PATH last: {lines!r}")
        other_writer(order_b)
        ok, lines = shape(order_b)
        if not ok:
            fails.append(f"the other writer after a resume: {lines!r}")
        old = os.path.join(d, "plugins", "cache", "m", "brain", "0.9.0", "tools")
        upgraded = os.path.join(d, "upgraded-env")
        with open(upgraded, "w") as fh:
            fh.write(f'export PATH="$PATH":{shlex.quote(old)}\nexport PATH="$PATH":{shlex.quote(other_tools)}\n')
        env["CLAUDE_ENV_FILE"] = upgraded
        hook("SessionStart")
        got = open(upgraded).read().splitlines()
        if any(old in ln for ln in got) or got.count(f'export PATH="$PATH":{shlex.quote(other_tools)}') != 1 \
                or got.count(mine) != 1 or got[-1] != mine:
            fails.append(f"an older brain version's PATH line goes, another plugin's stays: {got!r}")
        env["CLAUDE_ENV_FILE"] = env_file

        nl_file = os.path.join(d, "newline-env")
        env["CLAUDE_ENV_FILE"] = nl_file
        bad_cwd = "/a\n' ; touch /tmp/x ; '"
        for _ in range(2):
            hook("SessionStart", sid="nl-1", raw=json.dumps({"hook_event_name": "SessionStart", "session_id": "nl-1",
                                                             "cwd": bad_cwd}))
        nl_lines = open(nl_file).read().splitlines()
        if [ln for ln in nl_lines if "LEAN_SESSION_CWD" in ln] or \
                [ln for ln in nl_lines if ln != mine and not ln.startswith("export LEAN_SESSION_ID=")]:
            fails.append(f"a cwd with a newline must write no cwd line and leave only expected lines: {nl_lines!r}")
        if brain_out("session", "status", "nl-1") != "open":
            fails.append("a cwd with a newline must be stored as an empty cwd")
        env["CLAUDE_ENV_FILE"] = env_file

        # session record and answers
        if brain_out("session", "status", "abc-123") != f"open {cwd}":
            fails.append("start must record the session as open")
        hook("SessionEnd")
        if brain_out("session", "status", "abc-123") != f"ended {cwd}":
            fails.append("end must record the session as ended")
        subprocess.run([sys.executable, BRAIN, "add", "--project", "p", "--kind", "question", "--ref", "q",
                        "--title", "which port", "--session", "abc-123"], env=env, capture_output=True)
        subprocess.run([sys.executable, BRAIN, "close", "1", "--resolution", "answered", "--note", "8080"],
                       env=env, capture_output=True)
        rc, out, err = hook("UserPromptSubmit")
        try:
            ctx = json.loads(out)["hookSpecificOutput"]
        except (ValueError, KeyError):
            ctx = {}
        if rc != 0 or "#1 which port: 8080" not in ctx.get("additionalContext", "") \
                or ctx.get("hookEventName") != "UserPromptSubmit":
            fails.append(f"a prompt must carry the new answer: {rc} {out!r} {err!r}")
        if hook("UserPromptSubmit")[1]:
            fails.append("an answer is handed over once")
        if hook("UserPromptSubmit", sid="other-9")[1]:
            fails.append("another session gets no answer")

        # odd ids, bad events, switches
        before = open(env_file).read()
        rc, out, err = hook("SessionStart", sid="x; rm -rf ~")
        if rc != 0 or open(env_file).read() != before or brain_out("session", "status", "x; rm -rf ~") != "":
            fails.append("an odd session id must be ignored")
        odd_file = os.path.join(d, "odd-env")
        env["CLAUDE_ENV_FILE"] = odd_file
        hook("SessionStart", sid="x; rm -rf ~")
        if (open(odd_file).read() if os.path.exists(odd_file) else "") != mine + "\n":
            fails.append("an odd session id must still get the PATH line and nothing else")
        hook("SessionStart", raw=json.dumps({"hook_event_name": "SessionStart", "session_id": 12345, "cwd": cwd}))
        if brain_out("session", "status", "12345") != "unknown":
            fails.append("a numeric session id must be ignored")
        if hook("", raw="not json")[0] != 0 or hook("", raw="[1]")[0] != 0:
            fails.append("a bad event must not fail")
        off_file = os.path.join(d, "off-env")
        with open(off_file, "w") as fh:
            fh.write("".join(ln + "\n" for ln in seed("keep-1")))
        env["CLAUDE_ENV_FILE"] = off_file
        hook("SessionStart", sid="off-1", LEAN_SESSION_ENV="off")
        off_lines = open(off_file).read().splitlines()
        if off_lines != seed("keep-1") + [mine]:
            fails.append(f"LEAN_SESSION_ENV=off must still write brain's PATH line, and only that: {off_lines!r}")
        if brain_out("session", "status", "off-1") != "unknown":
            fails.append("LEAN_SESSION_ENV=off must not record the session")
        hook("SessionStart", sid="off-2", LEAN_SESSION_ENV="off")
        if open(off_file).read().splitlines() != off_lines:
            fails.append("a repeat with LEAN_SESSION_ENV=off must change nothing")
        subprocess.run([sys.executable, BRAIN, "add", "--project", "p", "--kind", "question", "--ref", "q2",
                        "--title", "which host", "--session", "abc-123"], env=env, capture_output=True)
        subprocess.run([sys.executable, BRAIN, "close", "2", "--resolution", "answered", "--note", "db1"],
                       env=env, capture_output=True)
        if hook("UserPromptSubmit", LEAN_SESSION_ENV="off")[1] or "which host" not in hook("UserPromptSubmit")[1]:
            fails.append("LEAN_SESSION_ENV=off must hand over no answers")
        off_env = os.path.join(d, "profile-off-env")
        env["CLAUDE_ENV_FILE"] = off_env
        for name in ("SessionStart", "SessionEnd", "UserPromptSubmit"):
            r = hook(name, sid="prof-1", LEAN_PROFILE="off")
            if r != (0, "", ""):
                fails.append(f"LEAN_PROFILE=off must print nothing for {name}: {r!r}")
        if os.path.exists(off_env) or brain_out("session", "status", "prof-1") != "unknown":
            fails.append("LEAN_PROFILE=off must write nothing")

        # env file trouble never stops the session record
        for label, bad, sid in (("a missing folder", os.path.join(d, "no-such-dir", "env"), "nodir-1"),
                                ("a read-only file", os.path.join(d, "ro-env"), "ro-1")):
            if label == "a read-only file":
                with open(bad, "w") as fh:
                    fh.write("export A=1\n")
                os.chmod(bad, 0o444)
            env["CLAUDE_ENV_FILE"] = bad
            rc, out, err = hook("SessionStart", sid=sid)
            if rc != 0 or err or brain_out("session", "status", sid) != f"open {cwd}":
                fails.append(f"{label} as the env file must not stop the session start: {rc} {err!r}")
        if open(os.path.join(d, "ro-env")).read() != "export A=1\n":
            fails.append("a read-only env file must be left alone")
        env.pop("CLAUDE_ENV_FILE")
        if hook("SessionStart", sid="noenv-1")[0] != 0 or brain_out("session", "status", "noenv-1") != f"open {cwd}":
            fails.append("no CLAUDE_ENV_FILE must still record the session")
        env["CLAUDE_ENV_FILE"] = env_file

        # a locked database: quick, quiet, exit 0
        locker = sqlite3.connect(env["LEAN_BRAIN_DB"], isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        began = time.monotonic()
        rc, out, err = hook("UserPromptSubmit")
        took = time.monotonic() - began
        locker.execute("ROLLBACK")
        locker.close()
        if rc != 0 or out or err or took > 8:
            fails.append(f"a locked database must give up quietly within the timeout: {rc} {out!r} {err!r} {took:.1f}s")

        # append_env on its own: atomic write, mode, CRLF, lock, odd files
        saved_env = os.environ.get("CLAUDE_ENV_FILE")
        saved_tools = bh.TOOLS
        try:
            atomic = os.path.join(d, "atomic-env")
            with open(atomic, "w") as fh:
                fh.write("export FOREIGN=1\r\nexport B=2\r\n")
            os.chmod(atomic, 0o640)
            inode = os.stat(atomic).st_ino
            os.environ["CLAUDE_ENV_FILE"] = atomic
            bh.append_env([mine], ours(False))
            if os.stat(atomic).st_ino == inode:
                fails.append("the env file must be written to a temp file and renamed over, never truncated in place")
            if open(atomic, newline="").read() != f"export FOREIGN=1\r\nexport B=2\r\n{mine}\r\n":
                fails.append(f"foreign lines must survive and CRLF must stay CRLF: {open(atomic, newline='').read()!r}")
            if os.stat(atomic).st_mode & 0o777 != 0o640:
                fails.append("the env file must keep its mode")
            if [n for n in os.listdir(d) if n.endswith(".tmp")]:
                fails.append("no temp file may be left beside the env file")
            glued = os.path.join(d, "glued-env")
            with open(glued, "w") as fh:
                fh.write("export A=1")
            os.environ["CLAUDE_ENV_FILE"] = glued
            for _ in range(3):
                bh.append_env(env_lines("glue-1", "/w", True), ours(True))
            got = subprocess.run(["bash", "-c", f'. {shlex.quote(glued)}; printf "%s|%s" "$A" "$LEAN_SESSION_ID"'],
                                 capture_output=True, text=True).stdout
            if got != "1|glue-1" or open(glued).read().count("\n") != 4:
                fails.append(f"a file with no final newline keeps its last line apart from ours, each of ours once: "
                             f"{open(glued).read()!r}")
            empty = subprocess.run(["bash", "-c", f'PATH=; . {shlex.quote(glued)}; printf %s "$PATH"'],
                                   capture_output=True, text=True).stdout
            if empty != bh.TOOLS:
                fails.append(f"an empty PATH must not gain an empty element: {empty!r}")
            spaced = os.path.join(d, "a dir", "tools")
            os.makedirs(spaced)
            with open(os.path.join(spaced, "hello"), "w") as fh:
                fh.write("#!/bin/sh\necho hi\n")
            os.chmod(os.path.join(spaced, "hello"), 0o755)
            spaced_file = os.path.join(d, "spaced-env")
            os.environ["CLAUDE_ENV_FILE"] = spaced_file
            bh.TOOLS = spaced
            bh.append_env([bh.path_line()], ours(False))
            got = subprocess.run(["bash", "-c", f"PATH=/usr/bin:/bin; . {shlex.quote(spaced_file)}; hello"],
                                 capture_output=True, text=True).stdout.strip()
            if got != "hi":
                fails.append(f"a tools folder with a space must be quoted: {got!r}")
            bh.TOOLS = saved_tools
            import fcntl
            raced = os.path.join(d, "raced-env")
            with open(raced, "w") as fh:
                fh.write("export A=1\n")
            env["CLAUDE_ENV_FILE"] = raced
            held = os.open(raced, os.O_RDONLY)
            fcntl.flock(held, fcntl.LOCK_EX)
            child = subprocess.Popen([sys.executable, me], stdin=subprocess.PIPE, env=env, text=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            child.stdin.write('{"hook_event_name": "SessionStart", "session_id": "race-1", "cwd": "/w"}')
            child.stdin.close()
            time.sleep(0.5)
            waited = open(raced).read() == "export A=1\n"
            with open(raced, "a") as fh:
                fh.write("export OTHER=1\n")  # another hook appends while we hold the lock
            os.close(held)
            child.wait()
            if not waited or "export OTHER=1\n" not in open(raced).read():
                fails.append(f"a second writer must wait for the lock and keep the line added meanwhile: "
                             f"{open(raced).read()!r}")
        finally:
            bh.TOOLS = saved_tools
            if saved_env is None:
                os.environ.pop("CLAUDE_ENV_FILE", None)
            else:
                os.environ["CLAUDE_ENV_FILE"] = saved_env

        # run(): any exception exits 0 with nothing on stdout and one line on stderr
        for label, body in (("an error", "print('half'); 1/0"), ("SystemExit 2", "print('half'); raise SystemExit(2)"),
                            ("a string exit", "raise SystemExit('bad')")):
            code = ("import sys; sys.path.insert(0, %r); from brain import hooks; hooks.run(lambda: exec(%r))"
                    % (LIB, body))
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            if r.returncode != 0 or r.stdout or not r.stderr.startswith("brain hook error ignored: ") \
                    or len(r.stderr.strip().splitlines()) != 1:
                fails.append(f"run() with {label} must exit 0, print nothing, and say one line on stderr: "
                             f"{r.returncode} {r.stdout!r} {r.stderr!r}")
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from brain import hooks; "
                            "hooks.run(lambda: print('kept'))" % LIB], capture_output=True, text=True)
        if r.returncode != 0 or r.stdout != "kept\n" or r.stderr:
            fails.append(f"run() must pass a good main's output through: {r.returncode} {r.stdout!r} {r.stderr!r}")
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from brain import hooks; "
                            "print(hooks.tool_path('brain'))" % LIB], capture_output=True, text=True,
                           env=dict(env, PATH="/usr/bin:/bin"))
        if r.stdout.strip() != shlex.quote(os.path.join(bh.TOOLS, "brain")):
            fails.append(f"tool_path off PATH must be the quoted absolute path: {r.stdout!r}")
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); from brain import hooks; "
                            "print(hooks.tool_path('brain'))" % LIB], capture_output=True, text=True,
                           env=dict(env, PATH=bh.TOOLS + os.pathsep + "/usr/bin"))
        if r.stdout.strip() != "brain":
            fails.append(f"tool_path with the tools folder on PATH must be the bare name: {r.stdout!r}")
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    bh.run(main)
