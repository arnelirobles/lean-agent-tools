"""The one module through which lean-agent-tools finds and runs `brain`.

brain is a separate plugin and may be missing or old. find_brain() looks on PATH, then in the plugin
cache under ~/.claude/plugins/cache/*/brain/<version>/tools/brain (newest version first), and accepts
the first candidate whose `--version` prints `brain X.Y.Z` at least MIN_VERSION. call() runs it and
returns stdout, or None with one stderr line when brain is missing or fails.

  brainlink.py --self-test
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

MIN_VERSION = (1, 0, 0)
VERSION_TIMEOUT = 5
PREFIX = "lean-agent-tools: brain failed: "

_cache = None
_warned = set()


class _OutOfTime(Exception):
    pass


def reset():
    global _cache
    _cache = None
    _warned.clear()


def _warn(msg):
    sys.stderr.write(msg.replace("\n", " ").strip() + "\n")


def _version_ok(path, env, deadline=None):
    limit = VERSION_TIMEOUT
    if deadline is not None:
        limit = min(limit, deadline - time.monotonic())
        if limit <= 0:
            raise _OutOfTime()
    try:
        r = subprocess.run([sys.executable, path, "--version"], capture_output=True, text=True,
                           timeout=limit, env=env)
    except OSError:
        return False
    except subprocess.TimeoutExpired:
        if limit < VERSION_TIMEOUT:
            raise _OutOfTime()
        return False
    m = re.match(r"brain (\d+)\.(\d+)\.(\d+)\s*$", r.stdout) if r.returncode == 0 else None
    return bool(m) and tuple(int(x) for x in m.groups()) >= MIN_VERSION


def _cache_candidates(home):
    found = []
    for p in glob.glob(os.path.join(home, ".claude", "plugins", "cache", "*", "brain", "*", "tools", "brain")):
        ver = os.path.basename(os.path.dirname(os.path.dirname(p)))
        if re.fullmatch(r"\d+(\.\d+)*", ver):
            found.append((tuple(int(x) for x in ver.split(".")), p))
    return [p for _, p in sorted(found, reverse=True)]


def find_brain(env=None, home=None, timeout=None):
    """Return the path of a usable brain, or None. With timeout, discovery stops when it runs out:
    one stderr line, None, and nothing is cached, so a later call with more time can still find it."""
    global _cache
    default = env is None and home is None
    if default and _cache:
        return _cache
    e = os.environ if env is None else env
    h = home if home is not None else e.get("HOME") or os.path.expanduser("~")
    cands = []
    on_path = shutil.which("brain", path=e.get("PATH", ""))
    if on_path:
        cands.append(on_path)
    cands += [p for p in _cache_candidates(h) if p not in cands]
    deadline = None if timeout is None else time.monotonic() + timeout
    for path in cands:
        try:
            ok = _version_ok(path, dict(e), deadline)
        except _OutOfTime:
            _warn(f"{PREFIX}discovery timed out after {timeout}s")
            return None
        if ok:
            if default:
                _cache = path
            return path
        if path not in _warned:
            _warned.add(path)
            _warn(f"lean-agent-tools: skipping brain at {path}: needs brain {'.'.join(map(str, MIN_VERSION))} or newer")
    return None


def call(args, stdin=None, timeout=None):
    start = time.monotonic()
    path = find_brain(timeout=timeout)
    if not path:
        return None
    if timeout is not None:
        timeout = max(timeout - (time.monotonic() - start), 0.01)
    try:
        r = subprocess.run([sys.executable, path] + list(args), input=stdin, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _warn(f"{PREFIX}timed out after {timeout}s running {' '.join(args)}")
        return None
    except OSError as e:
        _warn(f"{PREFIX}{e}")
        return None
    if r.returncode != 0:
        _warn(PREFIX + (r.stderr.strip() or f"exit {r.returncode}"))
        return None
    return r.stdout


def list_json(args):
    out = call(["list", "--json"] + list(args))
    if out is None:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if not isinstance(data, list):
        _warn(PREFIX + "list output is not a JSON array")
        return None
    return data


def self_test():
    import contextlib
    import io
    import tempfile
    import time
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from testq import fakebrain
    fails = []

    def check(name, ok):
        if not ok:
            fails.append(name)

    saved = dict(os.environ)

    def run(fn):
        """Run fn with stderr captured and a clean cache; return (result, stderr lines)."""
        reset()
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            res = fn()
        reset()
        return res, [ln for ln in buf.getvalue().splitlines() if ln]

    with tempfile.TemporaryDirectory() as d:
        empty = os.path.join(d, "empty")
        os.makedirs(empty)

        def cache_dir(home, ver):
            return os.path.join(home, ".claude", "plugins", "cache", "m", "brain", ver, "tools")

        # a good brain on PATH is found, nothing on stderr
        pdir = os.path.join(d, "good")
        good = fakebrain.install_fake(pdir, {})
        home0 = os.path.join(d, "home0")
        os.makedirs(home0)
        got, err = run(lambda: find_brain({"PATH": pdir}, home0))
        check("fake on PATH is found", got == good)
        check("good brain prints nothing", err == [])

        # an old brain on PATH is skipped with one line naming it, the cache one is used
        odir = os.path.join(d, "old")
        old = fakebrain.install_fake(odir, {"FAKE_BRAIN_VERSION": "exit2"})
        home1 = os.path.join(d, "home1")
        cached = fakebrain.install_fake(cache_dir(home1, "1.0.0"), {})
        got, err = run(lambda: find_brain({"PATH": odir}, home1))
        check("old on PATH skipped for the cache copy", got == cached)
        check("skip message is one line naming the path", len(err) == 1 and old in err[0])

        # cache versions compare as numbers
        home2 = os.path.join(d, "home2")
        fakebrain.install_fake(cache_dir(home2, "1.2.0"), {})
        new = fakebrain.install_fake(cache_dir(home2, "1.10.0"), {})
        got, err = run(lambda: find_brain({"PATH": empty}, home2))
        check("1.10.0 beats 1.2.0", got == new)

        # a version below MIN_VERSION is skipped too
        home3 = os.path.join(d, "home3")
        fakebrain.install_fake(cache_dir(home3, "0.9.0"), {"FAKE_BRAIN_VERSION": "brain 0.9.0"})
        got, err = run(lambda: find_brain({"PATH": empty}, home3))
        check("brain 0.9.0 is not accepted", got is None and len(err) == 1)

        # nothing anywhere: None, silent
        got, err = run(lambda: find_brain({"PATH": empty}, home0))
        check("nothing found returns None", got is None)
        check("nothing found prints nothing", err == [])

        # call and list_json through the process environment
        state = os.path.join(d, "state.json")
        log = os.path.join(d, "log")
        os.environ.update(PATH=pdir, HOME=home0, FAKE_BRAIN_STATE=state, FAKE_BRAIN_LOG=log)
        os.environ.pop("FAKE_BRAIN_SLEEP", None)
        os.environ.pop("FAKE_BRAIN_RAW", None)
        try:
            def roundtrip():
                nid = call(["add", "--kind", "note", "hello"])
                return nid, list_json(["--open"]), call(["notice", "add", "--hook", "h"], stdin="x")
            (nid, items, nid2), err = run(roundtrip)
            check("call returns stdout", nid is not None and nid.strip() == "1")
            check("list_json parses the items", isinstance(items, list) and len(items) == 1
                  and items[0]["title"] == "hello")
            check("unmodeled subcommand returns a fresh id", nid2 is not None and nid2.strip() == "2")
            check("call and list_json print nothing on success", err == [])
            logged = [json.loads(ln) for ln in open(log)]
            check("every call is logged", ["notice", "add", "--hook", "h"] in logged)

            # non-zero exit: None and one stderr line
            os.environ["FAKE_BRAIN_RAW"] = "not json"
            got, err = run(lambda: list_json([]))
            check("list_json on non-JSON returns None", got is None)
            check("list_json non-JSON writes one failed line", len(err) == 1 and err[0].startswith(PREFIX))
            os.environ.pop("FAKE_BRAIN_RAW")

            # timeout: None in under 2 s, one line naming the timeout
            slow = fakebrain.install_fake(os.path.join(d, "slow"), {"FAKE_BRAIN_SLEEP": "5"})
            os.environ["PATH"] = os.path.dirname(slow)
            t0 = time.time()
            got, err = run(lambda: call(["list", "--json"], timeout=1))
            check("timeout returns None", got is None)
            check("timeout returns within 2 seconds", time.time() - t0 < 2)
            check("timeout writes one line naming it", len(err) == 1 and err[0].startswith(PREFIX)
                  and "timed out" in err[0])

            # a slow probe counts against the call timeout and is not cached
            vslow = fakebrain.install_fake(os.path.join(d, "vslow"), {"FAKE_BRAIN_VERSION_SLEEP": "5"})
            os.environ["PATH"] = os.path.dirname(vslow)
            t0 = time.time()
            got, err = run(lambda: call(["list", "--json"], timeout=1))
            check("slow probe returns None within the call timeout", got is None and time.time() - t0 < 2)
            check("slow probe writes one failed line", len(err) == 1 and err[0].startswith(PREFIX)
                  and "timed out" in err[0])
            reset()
            with contextlib.redirect_stderr(io.StringIO()):
                find_brain(timeout=0.2)
            check("a timed-out discovery is not cached", _cache is None)
            reset()

            # --version is never slowed, so a slow brain is still found
            got, err = run(lambda: find_brain({"PATH": os.path.dirname(slow)}, home0))
            check("slow brain still answers --version at once", got == slow and err == [])
        finally:
            os.environ.clear()
            os.environ.update(saved)
            reset()
    print("self-test: ok" if not fails else "self-test failed:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    sys.stderr.write("usage: brainlink.py --self-test\n")
    sys.exit(2)
