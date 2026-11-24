#!/usr/bin/env python3
"""Record the pull requests, issues, releases and work items an agent creates.

PostToolUse hook on Bash, after gh pr/issue/release create, az repos pr create
or az boards work-item create. The URL comes from the command's output (gh
prints it on the last line, az prints JSON); no URL means the create failed and
nothing is recorded. The row goes to brain's `created` table, which the brain
web page lists. It never blocks.

  LEAN_CREATED_LOG=off   turn it off
  created-log.py --self-test
"""
import contextlib
import io
import json
import os
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "lib"))
import brainlink  # noqa: E402
import hookkit  # noqa: E402

HOOK = {
    "events": [
        {"event": "PostToolUse", "matcher": "Bash", "if": "Bash(gh *)", "group": 0, "order": 1},
        {"event": "PostToolUse", "matcher": "Bash", "if": "Bash(az *)", "group": 0, "order": 2},
    ],
    "timeout": 10,
    "mode": "process",
    "pack": "core",
    "guard": False,
    "default": {"strict": "note", "standard": "note"},
}

GH_KINDS = {("pr", "create"): "pr", ("issue", "create"): "issue", ("release", "create"): "release"}
GH_URL = re.compile(r"https?://[^/\s]+/([^/\s]+/[^/\s]+)/(?:(pull|issues)/(\d+)|releases/tag/\S+)$")


AZ_PATHS = {
    "pr": re.compile(r"(?:/[^/]+){1,2}/_git/[^/]+/pullrequest/\d+$"),
    "workitem": re.compile(r"(?:/[^/]+)*/(?:_workitems/edit|_apis/wit/workItems)/\d+$", re.I),
}


def forge_url(url, forge, kind):
    """True when URL is https, on a forge host, and has the path shape of KIND."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.query or parts.fragment or parts.username or parts.port:
        return False
    if forge == "github":
        gh_host = (os.environ.get("GH_HOST") or "").lower()
        if host != "github.com" and not (gh_host and host == gh_host):
            return False
        path = {"pr": r"/[^/]+/[^/]+/pull/\d+", "issue": r"/[^/]+/[^/]+/issues/\d+",
                "release": r"/[^/]+/[^/]+/releases/tag/[^/]+"}[kind]
        return re.fullmatch(path, parts.path) is not None
    if host != "dev.azure.com" and not host.endswith(".visualstudio.com"):
        return False
    return AZ_PATHS[kind].search(parts.path) is not None


def title_of(words):
    for i, w in enumerate(words):
        if w in ("--title", "-t") and i + 1 < len(words):
            return words[i + 1]
        if w.startswith("--title="):
            return w[len("--title="):]
    return ""


def azure_org_path(url):
    m = re.match(r"https?://dev\.azure\.com/([^/]+/[^/]+)", url or "")
    return m.group(1) if m else ""


def parse_gh(words, output):
    kind = GH_KINDS.get(tuple(words[1:3]))
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not kind or not lines:
        return None
    m = GH_URL.match(lines[-1])
    if not m:
        return None
    return {"kind": kind, "forge": "github", "repo": m.group(1),
            "number": int(m.group(3)) if m.group(3) else None, "url": lines[-1]}


def parse_az(words, output):
    area = tuple(words[1:4])
    if area not in (("repos", "pr", "create"), ("boards", "work-item", "create")):
        return None
    try:
        data = json.loads(output)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if area[0] == "repos":
        web = (data.get("repository") or {}).get("webUrl")
        number = data.get("pullRequestId")
        if not web or not isinstance(number, int):
            return None
        m = re.match(r"https?://dev\.azure\.com/([^/]+/[^/]+)/_git/([^/]+)", web)
        repo = f"{m.group(1)}/{m.group(2)}" if m else ""
        return {"kind": "pr", "forge": "azure", "repo": repo, "number": number,
                "url": f"{web}/pullrequest/{number}"}
    url = ((data.get("_links") or {}).get("html") or {}).get("href") or data.get("url")
    number = data.get("id")
    if not url or not isinstance(number, int):
        return None
    return {"kind": "workitem", "forge": "azure", "repo": azure_org_path(url), "number": number, "url": url}


def parse_created(words, output):
    """{kind, forge, repo, number, url} for a create command whose output carries a URL, else None."""
    if not words:
        return None
    tool = os.path.basename(words[0])
    found = parse_gh(words, output or "") if tool == "gh" else parse_az(words, output or "") if tool == "az" else None
    if not found or not forge_url(found["url"], found["forge"], found["kind"]):
        return None
    found["title"] = title_of(words)
    return found


def response_text(resp):
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return str(resp.get("stdout") or "")
    return ""


def record(command, cwd, output, session_id):
    """Save the first created item in COMMAND through the brain command; return its URLs. Never raises, never exits,
    and runs brain only when there is something to save."""
    found = None
    for seg in hookkit.segments(command, cwd):
        found = parse_created(seg.words, output)
        if found:
            break
    if not found:
        return []
    try:
        branch = hookkit.git(seg.cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
        args = ["created", "add", "--kind", found["kind"], "--forge", found["forge"]]
        args += hookkit.flag_args("--url", found["url"])
        if found["number"] is not None:
            args += ["--number", str(found["number"])]
        for name, value in (("--repo", found["repo"]), ("--branch", branch), ("--title", found["title"]),
                            ("--session", session_id), ("--cwd", seg.cwd)):
            if value:
                args += hookkit.flag_args(name, value)
        with contextlib.redirect_stderr(io.StringIO()):
            saved = brainlink.call(args, timeout=1)
    except (SystemExit, Exception):  # a missing or failing brain must not fail the tool call
        return []
    return [found["url"]] if saved is not None else []


def main():
    if os.environ.get("LEAN_CREATED_LOG") == "off":
        return 0
    event = hookkit.read_event()
    command = hookkit.tool_input(event).get("command") or ""
    if not re.search(r"\b(gh|az)\b", command):
        return 0
    record(command, event.get("cwd") or os.getcwd(), response_text(event.get("tool_response")),
           str(event.get("session_id") or ""))
    return 0


def self_test():
    import subprocess
    import tempfile
    import time

    failures = 0

    def check(name, got, want):
        nonlocal failures
        if got != want:
            print(f"FAIL {name}: got {got!r}, want {want!r}")
            failures += 1

    def parse(cmd, out):
        seg = hookkit.segments(cmd)
        return parse_created(seg[0].words, out) if seg else None

    def pick(d, *keys):
        return {k: d[k] for k in keys} if d else d

    keys = ("kind", "forge", "repo", "number")
    out = "Creating pull request for x into main\n\nhttps://github.com/o/r/pull/12\n"
    check("gh pr", pick(parse("gh pr create --fill", out), *keys),
          {"kind": "pr", "forge": "github", "repo": "o/r", "number": 12})
    check("gh issue", pick(parse("gh issue create -t Bug -b x", "https://github.com/o/r/issues/5\n"), *keys),
          {"kind": "issue", "forge": "github", "repo": "o/r", "number": 5})
    check("issue title", parse("gh issue create -t 'Fix it' -b x", "https://github.com/o/r/issues/5")["title"], "Fix it")
    check("long title", parse("gh pr create --title=\"A b\"", out)["title"], "A b")
    check("no title", parse("gh pr create --fill", out)["title"], "")
    check("gh release", pick(parse("gh release create v1", "https://github.com/o/r/releases/tag/v1\n"), *keys),
          {"kind": "release", "forge": "github", "repo": "o/r", "number": None})
    pr = '{"pullRequestId": 7, "repository": {"webUrl": "https://dev.azure.com/org/p/_git/r"}}'
    got = parse("az repos pr create --title T", pr)
    check("az pr", pick(got, *keys), {"kind": "pr", "forge": "azure", "repo": "org/p/r", "number": 7})
    check("az pr url", got["url"], "https://dev.azure.com/org/p/_git/r/pullrequest/7")
    wi = '{"id": 9, "url": "https://dev.azure.com/org/_apis/wit/workItems/9", "_links": {"html": {"href": "https://dev.azure.com/org/p/_workitems/edit/9"}}}'
    got = parse("az boards work-item create --title T --type Bug", wi)
    check("az work item", pick(got, *keys), {"kind": "workitem", "forge": "azure", "repo": "org/p", "number": 9})
    check("az work item url", got["url"], "https://dev.azure.com/org/p/_workitems/edit/9")
    check("az work item fallback url", parse("az boards work-item create", '{"id": 3, "url": "https://dev.azure.com/org/_apis/wit/workItems/3"}')["url"], "https://dev.azure.com/org/_apis/wit/workItems/3")
    check("gh pr list", parse("gh pr list", out), None)
    check("failed create", parse("gh pr create --fill", "a pull request already exists"), None)
    check("empty output", parse("gh pr create", ""), None)
    check("echo", parse("echo gh pr create", out), None)
    check("non-forge url", parse("gh pr create", "https://evil.example/o/r/pull/1"), None)
    check("http url", parse("gh pr create", "http://github.com/o/r/pull/1"), None)
    check("javascript url", parse("gh pr create", "javascript:alert(1)//github.com/o/r/pull/1"), None)
    check("wrong path for kind", parse("gh issue create", "https://github.com/o/r/pull/1"), None)
    check("host suffix trick", parse("gh pr create", "https://github.com.evil.example/o/r/pull/1"), None)
    check("az non-forge", parse("az boards work-item create", '{"id": 3, "url": "https://x/3"}'), None)
    check("az pr foreign host", parse("az repos pr create", '{"pullRequestId": 7, "repository": {"webUrl": "https://evil.example/org/p/_git/r"}}'), None)
    check("az bad json", parse("az repos pr create", "WARNING"), None)

    from testq import fakebrain

    def lines(path):
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def calls(path):
        return [c for c in lines(path) if c != ["--version"]]

    def brain_env(tmp, name, fake):
        """PATH holds only a fake brain (or nothing), HOME is empty, and no database path points at real data."""
        bin_dir, home = os.path.join(tmp, name + "-bin"), os.path.join(tmp, name + "-home")
        os.makedirs(bin_dir, exist_ok=True)
        os.makedirs(home, exist_ok=True)
        if fake is not None:
            fakebrain.install_fake(bin_dir, fake)
        return {"PATH": bin_dir, "HOME": home, "LEAN_BRAIN_DB": os.path.join(tmp, "brain.db"),
                "LEAN_TESTS_DB": os.path.join(tmp, "tests.db")}

    def run_hook(env, cmd, cwd, output):
        event = json.dumps({"session_id": "s1", "cwd": cwd, "tool_input": {"command": cmd},
                            "tool_response": {"stdout": output}})
        done = subprocess.run([sys.executable, os.path.realpath(__file__)], input=event, env=env,
                              capture_output=True, text=True)
        return done.returncode, done.stdout, done.stderr

    saved_env = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = "gh pr create --fill"
            url = "https://github.com/o/r/pull/12"
            log = os.path.join(tmp, "calls.log")
            base = {k: v for k, v in saved_env.items() if k not in ("LEAN_CREATED_LOG",)}

            os.environ.clear()
            os.environ.update(base)
            os.environ.update(brain_env(tmp, "none", None))
            brainlink.reset()
            check("no brain, a create saves nothing", record(cmd, tmp, out, "s1"), [])

            fake = {"FAKE_BRAIN_LOG": log}
            os.environ.update(brain_env(tmp, "fake", fake))
            brainlink.reset()
            check("record", record(cmd, tmp, out, "s1"), [url])
            check("record calls created add", calls(log), [[
                "created", "add", "--kind", "pr", "--forge", "github", "--url", url, "--number", "12",
                "--repo", "o/r", "--session", "s1", "--cwd", tmp]])
            check("record twice", record(cmd, tmp, out, "s1"), [url])
            check("a failed create makes no brain call", record(cmd, tmp, "nope", "s1"), [])
            check("a listing makes no brain call", record("gh pr list", tmp, out, "s1"), [])
            check("only the two creates reached brain", len(calls(log)), 2)
            check("a title is passed", record("gh issue create -t 'Fix it' -b x", tmp,
                                             "https://github.com/o/r/issues/5\n", "s1") and calls(log)[-1][-8:-4],
                  ["--repo", "o/r", "--title", "Fix it"])
            brainlink.reset()

            # the hook as a process: nothing extra on stdout or stderr, whatever brain does
            nolog = os.path.join(tmp, "hook.log")
            none_env = dict(base, **brain_env(tmp, "hook-none", None))
            want = run_hook(none_env, cmd, tmp, out)
            check("hook with no brain exits 0 and prints nothing", want, (0, "", ""))
            hook_env = dict(base, **brain_env(tmp, "hook-fake", {"FAKE_BRAIN_LOG": nolog}))
            check("hook with a brain prints the same", run_hook(hook_env, cmd, tmp, out), want)
            check("hook recorded one created add", [c[:8] for c in calls(nolog)],
                  [["created", "add", "--kind", "pr", "--forge", "github", "--url", url]])
            check("a second identical run prints nothing extra", run_hook(hook_env, cmd, tmp, out), want)
            check("the second run reached brain again", len(calls(nolog)), 2)
            slow_env = dict(base, **brain_env(tmp, "hook-slow", {"FAKE_BRAIN_SLEEP": "5"}))
            t0 = time.time()
            check("hook with a slow brain prints the same", run_hook(slow_env, cmd, tmp, out), want)
            check("hook with a slow brain returns within 4 s", time.time() - t0 < 4, True)
            old_env = dict(base, **brain_env(tmp, "hook-old", {"FAKE_BRAIN_VERSION": "exit2"}))
            check("hook with an old brain prints the same", run_hook(old_env, cmd, tmp, out), want)
            off_env = dict(hook_env, LEAN_CREATED_LOG="off")
            os.remove(nolog)
            check("LEAN_CREATED_LOG=off makes no brain call", (run_hook(off_env, cmd, tmp, out), lines(nolog)),
                  (want, []))
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        brainlink.reset()
    print("self-test: ok" if failures == 0 else f"self-test: {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    hookkit.run(main)
