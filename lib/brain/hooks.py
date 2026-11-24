"""What brain's hooks share: read the event, print context, append to the session env file, exit 0.

Standard library only. Nothing here imports the rest of brain or any other plugin.
"""
import contextlib
import io
import json
import os
import re
import shlex
import sys
import tempfile

TOOLS = os.path.normpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "tools"))

PATH_LINE = re.compile(r'export PATH=(?:"\$PATH":|"\$\{PATH:\+\$PATH:\}")(.+)')
SESSION_LINE = re.compile(r"export LEAN_SESSION_(?:ID|CWD)=")


def read_event():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    return event if isinstance(event, dict) else {}


def context(event_name, text):
    """Print text for Claude to read with the event. Prints only."""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}))
    sys.stdout.flush()


def off(switch=None):
    """True when LEAN_PROFILE is off, or the named switch variable is."""
    for name in ("LEAN_PROFILE", switch):
        if name and (os.environ.get(name) or "").strip().lower() == "off":
            return True
    return False


def on_path():
    return TOOLS in [os.path.normpath(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def tool_path(name):
    """How to run a brain tool: the bare name when the tools folder is on PATH, else its absolute path, quoted."""
    return name if on_path() else shlex.quote(os.path.join(TOOLS, name))


def path_line():
    """The PATH line for brain's tools folder. An empty PATH gets no leading colon, which would put the
    current folder on PATH. It appends, so a tool named like a system command does not shadow it."""
    return f'export PATH="${{PATH:+$PATH:}}"{shlex.quote(TOOLS)}'


def ours_path(line):
    """True for a PATH line brain wrote: for this tools folder, or for another brain version's in the plugin
    cache (<...>/plugins/cache/<marketplace>/brain/<version>/tools). Any other folder belongs to someone else."""
    m = PATH_LINE.fullmatch(line)
    if not m:
        return False
    try:
        parts = shlex.split(m.group(1))
    except ValueError:
        return False
    if len(parts) != 1:
        return False
    folder = os.path.normpath(parts[0])
    if folder == TOOLS:
        return True
    p = folder.split(os.sep)
    return len(p) >= 7 and p[-1] == "tools" and p[-3] == "brain" and p[-5] == "cache" and p[-6] == "plugins"


def _locked(env_file):
    """Open env_file (creating it) and hold an exclusive flock on it, so two hooks do not lose each other's
    lines. Returns the fd, or None where flock does not exist. Retries when the file was replaced while we
    waited, since the lock is on the old inode then."""
    try:
        import fcntl
    except ImportError:
        return None
    for _ in range(10):
        fd = os.open(env_file, os.O_RDONLY | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_ino == os.stat(env_file).st_ino:
                return fd
        except OSError:
            pass
        os.close(fd)
    raise OSError("env file keeps changing")


def _replace(env_file, text, mode):
    """Write text to a temp file beside env_file and rename it over, so a crash never leaves half a file."""
    fd, tmp = tempfile.mkstemp(prefix=".brain-env-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(env_file)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, env_file)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_env(lines, ours):
    """Put lines last in $CLAUDE_ENV_FILE, after dropping the lines `ours` matches (an earlier run of the
    same hook, as on resume and compact). Other lines stay. The read and the write happen under a lock, the
    write goes through a temp file and a rename, and the file keeps its mode and its line endings. A
    read-only or unreadable file, or no CLAUDE_ENV_FILE, is left alone."""
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    if os.path.exists(env_file) and not os.access(env_file, os.W_OK):
        return  # a read-only file is someone's choice, a rename would get around it
    try:
        lock = _locked(env_file)
    except OSError:
        return
    try:
        try:
            with open(env_file, encoding="utf-8", newline="") as f:
                content = f.read()
            st_mode = os.stat(env_file).st_mode & 0o7777
        except FileNotFoundError:  # no flock here, so nothing created it
            umask = os.umask(0)
            os.umask(umask)
            content, st_mode = "", 0o666 & ~umask
        except (OSError, UnicodeDecodeError):
            return  # never overwrite a file we could not read
        nl = "\r\n" if "\r\n" in content else "\n"
        have = content.split(nl)
        if have[-1] == "":
            have.pop()
        text = "".join(ln + nl for ln in [ln for ln in have if not ours(ln)] + list(lines))
        if text != content:
            try:
                _replace(env_file, text, st_mode)
            except OSError:
                return
    finally:
        if lock is not None:
            os.close(lock)


def run(main):
    """Run a hook's main and exit 0. Whatever main raises, SystemExit included, leaves stdout empty and one
    line on stderr; a hook never fails the session."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(f"brain hook error ignored: {exc!r}", file=sys.stderr)
            sys.exit(0)
    except BaseException as exc:
        print(f"brain hook error ignored: {exc!r}", file=sys.stderr)
        sys.exit(0)
    sys.stdout.write(out.getvalue())
    sys.stdout.flush()
    sys.exit(0)
