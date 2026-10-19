"""Approve or deny a Claude Code permission request from the board.

OPT-IN, AND OFF BY DEFAULT. `blink approve enable` installs the hook and
creates ~/.blink/approve; until then none of this runs. It is a separate
switch because it widens what Blink handles: the state hook (tools/blink-hook.sh)
promises to read no tool arguments at all, and this reads one -- the command or
path Claude Code is asking permission for -- because a yes or a no means
nothing to a person who was not shown what it is for.

How a request travels, all of it on this machine and over the board's cable:

    Claude Code --stdin--> `blink approve-hook`          (run_hook)
        writes ~/.blink/approve/<id>.req, then polls for <id>.res
    daemon (Approvals.poll) sees the .req, sends `approve_req` to the board
    board shows Allow / Deny; a tap sends `approve_res`
    daemon (Approvals.on_result) writes <id>.res
    the hook prints the decision and exits

The rule that governs every line below: THE BOARD CAN SAY YES OR NO, AND IT CAN
NEVER BE THE REASON A QUESTION GOES UNASKED. Anything that is not a clean tap --
no board, no daemon, a timeout, a command too long to show whole, a second
request while one is open, any exception -- ends with the hook printing nothing
and exiting 0, which Claude Code reads as "no decision" and asks at the
keyboard exactly as it would have without Blink. There is no path from an error
to an "allow".

What is shown is what is decided. A request is sent to the board only when its
full text is printable ASCII and fits the card (MAX_TEXT_CHARS). Non-ASCII is
refused rather than drawn because a lookalike letter in a URL or a path is the
thing a careful person would want to catch and a small screen would hide.

The text is never logged. It is a command line; it can contain a token. The
daemon's log gets the id and the tool name, and nothing else.

Both processes run as the same user, so a file in ~/.blink/approve is as
trustworthy as the user is. The directory is refused when it is not owned by
this user or is writable by anyone else: on a shared machine that would let
another account forge a .res.
"""
import json
import os
import re
import secrets
import signal
import sys
import time

from pc import protocol

SUBDIR = "approve"
READY_NAME = "ready"

# What the card can show whole: six lines of about 37 characters, less the
# loss at word breaks. Measured against the card's 280 px box at the default
# font; a longer command is asked at the keyboard instead.
MAX_TEXT_CHARS = 180
MAX_TEXT_LINES = 6

# How long the hook waits for a tap before giving the question back to the
# keyboard. Claude Code's own `timeout` for the hook is left unset on purpose
# (its unit is documented inconsistently and the default is minutes); this is
# the limit that matters, and it is well below any reading of that default.
# Whether Claude Code shows its own prompt WHILE the hook waits is not
# documented, so keep this short enough that a person who walks to the
# keyboard is not made to wait on it.
DEFAULT_WAIT_S = 30.0
MAX_WAIT_S = 120.0

# The daemon touches `ready` while a board that can show a request is
# connected. A hook that finds it older than this does not even write a
# request: it exits at once and the keyboard asks.
READY_FRESH_S = 15.0
READY_TOUCH_EVERY_S = 1.0
POLL_S = 0.25
DAEMON_POLL_EVERY_S = 0.3
# A request file older than its ttl plus this is debris from a hook that died.
STALE_GRACE_S = 10.0

# Tools whose permission prompts can be answered from the board, and which key
# of tool_input holds the thing being approved. Bash shows the command whole.
# The file tools show the PATH, not the edit: say so in the docs, because
# approving a write by its path alone is a smaller promise than approving a
# command. Everything else (MCP tools, subagents, plan mode) is asked at the
# keyboard.
TOOL_KEYS = {
    "Bash": "command",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "Read": "file_path",
    "NotebookEdit": "notebook_path",
    "WebFetch": "url",
}
TOOL_MATCHER = "|".join(TOOL_KEYS)

DECISIONS = ("allow", "deny", "decline")
_ID = re.compile(r"\A[0-9a-f]{8}\Z")
_MARKER = "~/.blink/approve-installed-command"


def approve_dir():
    # A function, not a module constant: an expanduser() at import time is
    # evaluated before any test's HOME redirect and would point at the real
    # home (see tests/conftest.py).
    return os.path.join(os.path.expanduser("~"), ".blink", SUBDIR)


def dir_safe(d):
    """The directory exists, is ours, and nobody else can write to it."""
    try:
        st = os.stat(d)
    except OSError:
        return False
    if sys.platform == "win32":
        return os.path.isdir(d)
    return (os.path.isdir(d) and st.st_uid == os.getuid()
            and (st.st_mode & 0o022) == 0)


def displayable(text):
    """True when `text` can be shown on the card exactly as it is."""
    if not isinstance(text, str) or not text.strip():
        return False
    if len(text) > MAX_TEXT_CHARS or text.count("\n") + 1 > MAX_TEXT_LINES:
        return False
    return all(c == "\n" or " " <= c <= "~" for c in text)


def summarize(tool_name, tool_input):
    """(tool, text) for a request the board may be asked about, else None."""
    key = TOOL_KEYS.get(tool_name) if isinstance(tool_name, str) else None
    if key is None or not isinstance(tool_input, dict):
        return None
    text = tool_input.get(key)
    if not displayable(text):
        return None
    return tool_name, text


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_private(path, data):
    """Create `path` atomically with mode 0600, or raise OSError."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _read_text(path, limit=65536):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit)
    except (OSError, ValueError):
        return None


def daemon_ready(d, now):
    try:
        return 0 <= now - os.stat(os.path.join(d, READY_NAME)).st_mtime \
            <= READY_FRESH_S
    except OSError:
        return False


# --------------------------------------------------------------- the hook

def _decision_json(decision):
    if decision == "allow":
        body = {"behavior": "allow"}
    else:
        body = {"behavior": "deny", "message": "Denied from the Blink board."}
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "decision": body}})


def run_hook(stdin_text, out, d=None, now=time.time, sleep=time.sleep,
             wait_s=None, make_id=None):
    """The `blink approve-hook` command. Always returns 0.

    `out` receives the decision JSON, or nothing at all.
    """
    req_path = res_path = None
    try:
        d = d or approve_dir()
        if wait_s is None:
            try:
                wait_s = float(os.environ.get("BLINK_APPROVE_WAIT_S", ""))
            except ValueError:
                wait_s = DEFAULT_WAIT_S
        wait_s = min(max(wait_s, 1.0), MAX_WAIT_S)

        if len(stdin_text) > 1_000_000:
            return 0
        try:
            payload = json.loads(stdin_text)
        except ValueError:
            return 0
        if not isinstance(payload, dict) or \
                payload.get("hook_event_name") != "PermissionRequest":
            return 0
        found = summarize(payload.get("tool_name"), payload.get("tool_input"))
        if found is None or not dir_safe(d) or not daemon_ready(d, now()):
            return 0
        tool, text = found

        req_id = (make_id or (lambda: secrets.token_hex(4)))()
        if not _ID.match(req_id):
            return 0
        req_path = os.path.join(d, req_id + ".req")
        res_path = os.path.join(d, req_id + ".res")
        _write_private(req_path, json.dumps(
            {"id": req_id, "tool": tool, "text": text, "t": now(),
             "ttl": wait_s}))

        deadline = now() + wait_s
        decision = None
        while now() < deadline:
            word = _read_text(res_path, 16)
            if word is not None and word.strip() in DECISIONS:
                decision = word.strip()
                break
            if not daemon_ready(d, now()):
                break        # the daemon or the board went away
            sleep(POLL_S)

        if decision in ("allow", "deny"):
            out.write(_decision_json(decision) + "\n")
            out.flush()
        return 0
    except Exception:
        # Not even a message: this runs inside Claude Code's permission flow,
        # and Blink having a bad day must not become the user's.
        return 0
    finally:
        for p in (req_path, res_path):
            if p:
                _unlink(p)


def hook_main():
    """Entry point for `blink approve-hook`."""
    # Claude Code kills a hook that outlives its timeout; run the cleanup in
    # run_hook's `finally` instead of leaving the request behind.
    def _bail(_signum, _frame):
        raise SystemExit(0)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _bail)
            except (ValueError, OSError):
                pass
    try:
        text = sys.stdin.read(1_000_001)
    except Exception:
        return 0
    return run_hook(text, sys.stdout)


# ------------------------------------------------------------- the daemon

class Approvals:
    """Carries requests from ~/.blink/approve to the board and back."""

    def __init__(self, write_msg, now=time.time, mono=time.monotonic,
                 dir_fn=approve_dir):
        self._write = write_msg
        self._now = now
        self._mono = mono
        self._dir = dir_fn
        self._state = {}          # id -> ("offered"|"answered", deadline)
        self._next_poll = 0.0
        self._last_touch = 0.0

    def resend(self):
        """The board restarted or reconnected: offer what is still open again."""
        self._state.clear()

    def poll(self, usable):
        """Look for requests. `usable` is: board connected and able to show one.

        Cheap when the feature is off (one stat), and rate-limited either way.
        """
        t = self._mono()
        if t < self._next_poll:
            return
        self._next_poll = t + DAEMON_POLL_EVERY_S
        d = self._dir()
        if not dir_safe(d):
            self._state.clear()
            return
        ready = os.path.join(d, READY_NAME)
        # With no usable board nothing is offered, but the sweep below still
        # runs: a hook killed mid-question leaves its .req (the command text)
        # behind, and that file must not wait for a board to reappear.
        offering = usable
        if not usable:
            _unlink(ready)
            self._state.clear()
        elif t - self._last_touch >= READY_TOUCH_EVERY_S:
            try:
                with open(ready, "a", encoding="utf-8"):
                    pass
                os.utime(ready, None)
                self._last_touch = t
            except OSError:
                return

        now = self._now()
        seen = set()
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return
        for name in names:
            if not name.endswith(".req"):
                continue
            req_id = name[:-4]
            if not _ID.match(req_id):
                continue
            path = os.path.join(d, name)
            try:
                req = json.loads(_read_text(path) or "")
                age = now - float(req["t"])
                ttl = min(max(float(req["ttl"]), 1.0), MAX_WAIT_S)
            except (ValueError, KeyError, TypeError):
                # Unreadable: nobody will answer it. Left a minute, then swept.
                try:
                    if now - os.stat(path).st_mtime > 60:
                        _unlink(path)
                except OSError:
                    pass
                continue
            if age > ttl + STALE_GRACE_S:
                _unlink(path)
                continue
            seen.add(req_id)
            if age > ttl:
                continue                  # the hook has stopped waiting
            if offering and req_id not in self._state:
                self._offer(d, req_id, req, ttl - max(age, 0.0))

        for req_id in list(self._state):
            kind, deadline = self._state[req_id]
            if req_id not in seen or now > deadline:
                if kind == "offered":
                    self._write(protocol.approve_end(req_id))
                del self._state[req_id]
                # The hook has stopped waiting, so an answer written after
                # this point has no reader. A tap that landed in the gap
                # leaves its .res behind; nothing else would remove it.
                _unlink(os.path.join(d, req_id + ".res"))

    def _offer(self, d, req_id, req, ttl_left):
        # The tool comes off disk, so it may not be a string at all. A list
        # here is unhashable, and the TypeError would escape poll() into the
        # daemon's read loop. summarize() already rejects a non-string tool.
        tool = req.get("tool")
        key = TOOL_KEYS.get(tool, "") if isinstance(tool, str) else ""
        found = summarize(tool, {key: req.get("text")})
        msg = None
        if found is not None:
            msg = protocol.approve_req(req_id, found[0], found[1],
                                       max(int(ttl_left), 1))
            raw, why = protocol.encode_checked(msg)
            if raw is None:
                msg = None
        if msg is None:
            # Not shown whole, so not shown: the keyboard asks.
            self._decide(d, req_id, "decline")
            print(f"[approve] {req_id} cannot be shown on the board;"
                  " asking at the computer", file=sys.stderr)
            return
        self._state[req_id] = ("offered", self._now() + ttl_left)
        self._write(msg)
        # The id and the tool, never the text. See the module docstring.
        print(f"[approve] request {req_id} ({found[0]})", file=sys.stderr)

    def _decide(self, d, req_id, word):
        try:
            _write_private(os.path.join(d, req_id + ".res"), word)
        except OSError:
            return
        self._state[req_id] = ("answered", self._now() + MAX_WAIT_S)

    def on_result(self, msg):
        """The board's answer."""
        req_id = msg.get("id")
        entry = self._state.get(req_id) if isinstance(req_id, str) else None
        if entry is None or entry[0] != "offered":
            return
        word = {1: "allow", 0: "deny", 2: "decline"}.get(msg.get("ok"))
        if word is None:
            return
        self._decide(self._dir(), req_id, word)
        print(f"[approve] {req_id} {word}", file=sys.stderr)


# ------------------------------------------------- settings.json install

def _marker_path():
    return os.path.expanduser(_MARKER)


def hook_command(bin_path):
    if sys.platform == "win32":
        return f'"{bin_path}" approve-hook'
    import shlex
    return f"{shlex.quote(bin_path)} approve-hook"


def _is_ours(command, current):
    if command == current:
        return True
    try:
        with open(_marker_path(), encoding="utf-8") as f:
            return command == f.read().strip()
    except OSError:
        return False


def enabled(settings_path, bin_path):
    from pc.install_statusline import SettingsUnreadable, _load
    try:
        data = _load(settings_path)
    except SettingsUnreadable:
        return False
    groups = (data.get("hooks") or {}).get("PermissionRequest")
    cmd = hook_command(bin_path)
    return isinstance(groups, list) and any(
        isinstance(g, dict) and any(
            isinstance(h, dict) and _is_ours(h.get("command", ""), cmd)
            for h in (g.get("hooks") or []))
        for g in groups)


def enable(settings_path, bin_path):
    """Install the hook and create the request directory. Idempotent."""
    from pc import install_hooks
    from pc.install_statusline import _load, _save, _sniff_format
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    command = hook_command(bin_path)
    entries = install_hooks._entries_for(data, "PermissionRequest")
    ours = next((h for g in entries if isinstance(g, dict)
                 for h in (g.get("hooks") or [])
                 if isinstance(h, dict)
                 and _is_ours(h.get("command", ""), command)), None)
    if ours is not None:
        ours["command"] = command      # repoint: the binary may have moved
    else:
        entries.append({"matcher": TOOL_MATCHER,
                        "hooks": [{"type": "command", "command": command}]})
    d = approve_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    _save(settings_path, data, indent, trailing_newline)
    os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
    with open(_marker_path(), "w", encoding="utf-8") as f:
        f.write(command + "\n")
    return "Board approvals enabled."


def disable(settings_path, bin_path=None):
    """Remove only our hook entry and the request directory."""
    from pc.install_statusline import (SettingsUnreadable, _load, _save,
                                       _sniff_format)
    removed = 0
    try:
        data = _load(settings_path)
    except SettingsUnreadable:
        return "settings.json could not be read; left it alone."
    command = hook_command(bin_path) if bin_path else ""
    hooks = data.get("hooks")
    groups = hooks.get("PermissionRequest") if isinstance(hooks, dict) else None
    if isinstance(groups, list):
        kept_groups = []
        for g in groups:
            inner = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(inner, list):
                kept_groups.append(g)
                continue
            kept = [h for h in inner if not (
                isinstance(h, dict) and _is_ours(h.get("command", ""), command))]
            removed += len(inner) - len(kept)
            if kept:
                g["hooks"] = kept
                kept_groups.append(g)
        if kept_groups:
            hooks["PermissionRequest"] = kept_groups
        else:
            hooks.pop("PermissionRequest", None)
        if not hooks:
            data.pop("hooks", None)
        indent, trailing_newline = _sniff_format(settings_path)
        _save(settings_path, data, indent, trailing_newline)
    _unlink(_marker_path())
    d = approve_dir()
    if os.path.isdir(d):
        for name in os.listdir(d):
            _unlink(os.path.join(d, name))
        try:
            os.rmdir(d)
        except OSError:
            pass
    return ("Board approvals disabled." if removed
            else "Board approvals were not enabled.")
