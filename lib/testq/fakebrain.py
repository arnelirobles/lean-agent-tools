"""A stand-in `brain` for tests.

State is a JSON file at $FAKE_BRAIN_STATE ({"next_id": N, "items": [...]}). Each call appends its argv
as one JSON line to $FAKE_BRAIN_LOG. Environment:
  FAKE_BRAIN_VERSION  text printed for --version (default "brain 1.0.0"); "exit2" mimics an old brain
  FAKE_BRAIN_SLEEP    seconds to sleep before any call except --version
  FAKE_BRAIN_EXIT     exit code for any call except --version, with one line on stderr (a brain that fails)
  FAKE_BRAIN_VERSION_SLEEP  seconds to sleep before answering --version (a slow probe)
  FAKE_BRAIN_RAW      printed verbatim by `list --json` instead of the items
  FAKE_BRAIN_STDIN_LOG  file that gets each unmodelled call's stdin as one JSON string per line
Modeled: --version, add, list --json, close, session infer --json. Any other subcommand reads stdin,
logs argv, prints a fresh integer id and exits 0.
"""
import json
import os
import sys
import time

LIB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(path):
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("next_id", 1)
    state.setdefault("items", [])
    return state


def _save(path, state):
    if path:
        with open(path, "w") as f:
            json.dump(state, f)


def _fresh_id(state):
    n = state["next_id"]
    state["next_id"] = n + 1
    return n


def _flags(args):
    flags, rest, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                flags[a[2:]] = args[i + 1]
                i += 2
                continue
            flags[a[2:]] = True
        else:
            rest.append(a)
        i += 1
    return flags, rest


def main(argv, stdin=None):
    env = os.environ
    log = env.get("FAKE_BRAIN_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(argv) + "\n")
    if argv == ["--version"]:
        vdelay = env.get("FAKE_BRAIN_VERSION_SLEEP")
        if vdelay:
            time.sleep(float(vdelay))
        text = env.get("FAKE_BRAIN_VERSION", "brain 1.0.0")
        if text == "exit2":
            sys.stderr.write("brain: error: unrecognized arguments: --version\n")
            return 2
        print(text)
        return 0
    delay = env.get("FAKE_BRAIN_SLEEP")
    if delay:
        time.sleep(float(delay))
    code = env.get("FAKE_BRAIN_EXIT")
    if code:
        sys.stderr.write("brain: fake failure\n")
        return int(code)
    path = env.get("FAKE_BRAIN_STATE")
    state = _load(path) if path else {"next_id": 1, "items": []}
    cmd = argv[0] if argv else ""
    if cmd == "list" and "--json" in argv:
        raw = env.get("FAKE_BRAIN_RAW")
        if raw is not None:
            print(raw)
            return 0
        flags, _ = _flags(argv[1:])
        items = state["items"]
        if flags.get("open"):
            items = [i for i in items if i.get("status") == "open"]
        for key in ("kind", "project", "cycle", "status"):
            if isinstance(flags.get(key), str):
                items = [i for i in items if str(i.get(key)) == flags[key]]
        if isinstance(flags.get("tag"), str):
            items = [i for i in items if flags["tag"] in (i.get("tags") or [])]
        print(json.dumps(items))
        return 0
    if cmd == "add":
        flags, rest = _flags(argv[1:])
        item = {"id": _fresh_id(state), "kind": "note", "status": "open", "project": None, "repo": None,
                "branch": None, "cycle": None, "run_id": None, "ref": None,
                "title": " ".join(rest), "tags": [], "first_seen": 0, "last_seen": 0}
        for key in ("kind", "status", "project", "repo", "branch", "cycle", "run_id", "ref", "title"):
            if isinstance(flags.get(key), str):
                item[key] = flags[key]
        if isinstance(flags.get("tag"), str):
            item["tags"] = sorted(flags["tag"].split(","))
        state["items"].append(item)
        _save(path, state)
        print(item["id"])
        return 0
    if cmd == "close" and len(argv) >= 2:
        for item in state["items"]:
            if str(item["id"]) == argv[1]:
                item["status"] = "closed"
        _save(path, state)
        return 0
    if argv[:2] == ["session", "infer"] and "--json" in argv:
        print(json.dumps({"id": "fake-session", "cwd": os.getcwd()}))
        return 0
    text = (stdin or sys.stdin).read()
    stdin_log = env.get("FAKE_BRAIN_STDIN_LOG")
    if stdin_log:
        with open(stdin_log, "a") as f:
            f.write(json.dumps(text) + "\n")
    print(_fresh_id(state))
    _save(path, state)
    return 0


def install_fake(dir, env):
    """Write an executable `brain` into dir and return its path.

    Every FAKE_BRAIN_* value in env is baked into the script as a default, so one fake can be an old
    or slow brain without touching the process environment.
    """
    os.makedirs(dir, exist_ok=True)
    path = os.path.join(dir, "brain")
    baked = {k: v for k, v in env.items() if k.startswith("FAKE_BRAIN_")}
    lines = [
        f"#!{sys.executable}",
        "import os, sys",
        f"sys.path.insert(0, {LIB_DIR!r})",
        f"for k, v in {baked!r}.items():",
        "    os.environ.setdefault(k, v)",
        "from testq import fakebrain",
        "sys.exit(fakebrain.main(sys.argv[1:]))",
        "",
    ]
    with open(path, "w") as f:
        f.write("\n".join(lines))
    os.chmod(path, 0o755)
    return path
