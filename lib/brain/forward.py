import glob
import os
import re
import shutil
import subprocess
import sys

MOVED = {"queue", "run-add", "flaky-clear"}


def _cache_candidates(home):
    found = []
    pattern = os.path.join(home, ".claude", "plugins", "cache", "*", "lean-agent-tools", "*", "tools", "test-phase")
    for p in glob.glob(pattern):
        ver = os.path.basename(os.path.dirname(os.path.dirname(p)))
        if re.fullmatch(r"\d+(\.\d+)*", ver) and os.path.isfile(p) and os.access(p, os.X_OK):
            found.append((tuple(int(x) for x in ver.split(".")), p))
    return [p for _, p in sorted(found, reverse=True)]


def find_test_phase():
    on_path = shutil.which("test-phase")
    if on_path:
        return on_path
    home = os.environ.get("HOME") or os.path.expanduser("~")
    cands = _cache_candidates(home)
    return cands[0] if cands else None


def forward(argv):
    """Run `test-phase` with the same arguments. The queue, run history and flaky clearing live there now."""
    moved = f'brain: "brain {argv[0]}" moved to "test-phase {argv[0]}"'
    path = find_test_phase()
    if not path:
        print(moved + ", and test-phase was not found", file=sys.stderr)
        return 2
    print(moved, file=sys.stderr)
    try:
        return subprocess.run([path, *argv]).returncode
    except OSError:
        print(moved + ", and test-phase was not found", file=sys.stderr)
        return 2
