"""Approving from the board: it may say yes or no, and may never be the reason
a question goes unasked. Everything here is generated; nothing is captured."""
import io
import json
import os

import pytest

from pc import approvals, protocol
from pc.approvals import Approvals, displayable, run_hook, summarize

NOW = 1_800_000_000.0
SECRET = "hunter2-token-9f8e7d"


def _d(tmp_path, mode=0o700):
    d = tmp_path / "approve"
    d.mkdir()
    os.chmod(d, mode)
    return str(d)


def _ready(d, at=NOW):
    p = os.path.join(d, "ready")
    open(p, "w").close()
    os.utime(p, (at, at))


def _payload(tool="Bash", tool_input=None, event="PermissionRequest"):
    return json.dumps({"hook_event_name": event, "session_id": "s1",
                       "tool_name": tool,
                       "tool_input": tool_input or {"command": "ls -la"}})


class _Clock:
    """A clock that only the fake sleep moves, so no test waits for real."""

    def __init__(self, t=NOW, on_sleep=None):
        self.t = t
        self.on_sleep = on_sleep

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep(self)


def _hook(d, payload, clock=None, wait_s=5.0, make_id=lambda: "ab12cd34"):
    clock = clock or _Clock()
    out = io.StringIO()
    rc = run_hook(payload, out, d=d, now=clock.now, sleep=clock.sleep,
                  wait_s=wait_s, make_id=make_id)
    return rc, out.getvalue()


# --- what may be shown at all ------------------------------------------------

def test_a_plain_command_is_displayable():
    assert displayable("git status --short")
    assert displayable("a\nb")


@pytest.mark.parametrize("text", [
    "", "   ", None, 5, "x" * 181, "\n".join("l" * 3 for _ in range(7)),
    "café", "аpple.com",       # a Cyrillic a: the lookalike case
    "tab\there", "bell\a", "nul\x00x",
])
def test_anything_that_cannot_be_shown_whole_is_refused(text):
    assert not displayable(text)


def test_only_the_listed_tools_with_their_one_field():
    assert summarize("Bash", {"command": "ls"}) == ("Bash", "ls")
    assert summarize("Write", {"file_path": "/tmp/x"}) == ("Write", "/tmp/x")
    assert summarize("WebFetch", {"url": "https://example.com"})
    assert summarize("mcp__x__y", {"command": "ls"}) is None
    assert summarize("Bash", {"cmd": "ls"}) is None
    assert summarize("Bash", "ls") is None


# --- the hook: every failure is silence --------------------------------------

def test_allow_prints_the_allow_decision(tmp_path):
    d = _d(tmp_path)
    _ready(d)

    def answer(clock):
        with open(os.path.join(d, "ab12cd34.res"), "w") as f:
            f.write("allow")
        _ready(d, clock.t)

    rc, out = _hook(d, _payload(), _Clock(on_sleep=answer))
    assert rc == 0
    body = json.loads(out)["hookSpecificOutput"]
    assert body["hookEventName"] == "PermissionRequest"
    assert body["decision"] == {"behavior": "allow"}


def test_deny_prints_the_deny_decision(tmp_path):
    d = _d(tmp_path)
    _ready(d)

    def answer(clock):
        with open(os.path.join(d, "ab12cd34.res"), "w") as f:
            f.write("deny")
        _ready(d, clock.t)

    rc, out = _hook(d, _payload(), _Clock(on_sleep=answer))
    assert json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_decline_is_silence(tmp_path):
    d = _d(tmp_path)
    _ready(d)

    def answer(clock):
        with open(os.path.join(d, "ab12cd34.res"), "w") as f:
            f.write("decline")
        _ready(d, clock.t)

    assert _hook(d, _payload(), _Clock(on_sleep=answer)) == (0, "")


def test_nothing_answers_then_silence_and_no_debris(tmp_path):
    d = _d(tmp_path)
    _ready(d)
    clock = _Clock(on_sleep=lambda c: _ready(d, c.t))
    assert _hook(d, _payload(), clock, wait_s=3.0) == (0, "")
    assert clock.t >= NOW + 3.0
    assert os.listdir(d) == ["ready"]


def test_no_daemon_means_no_wait_and_no_request(tmp_path):
    d = _d(tmp_path)           # no ready file
    clock = _Clock()
    assert _hook(d, _payload(), clock) == (0, "")
    assert clock.t == NOW and os.listdir(d) == []


def test_a_stale_ready_file_is_no_daemon(tmp_path):
    d = _d(tmp_path)
    _ready(d, NOW - 60)
    assert _hook(d, _payload()) == (0, "")
    assert os.listdir(d) == ["ready"]


def test_the_wait_ends_early_when_the_daemon_goes_away(tmp_path):
    d = _d(tmp_path)
    _ready(d)                  # fresh at the start, never touched again
    clock = _Clock()
    assert _hook(d, _payload(), clock, wait_s=60.0) == (0, "")
    assert clock.t < NOW + 30


@pytest.mark.parametrize("payload", [
    "not json", "[]", "null", "{}",
    _payload(event="PreToolUse"),
    _payload(tool="mcp__srv__do", tool_input={"command": "ls"}),
    _payload(tool_input={"command": "café"}),
    _payload(tool_input={"command": "x" * 500}),
    _payload(tool_input={"command": ""}),
])
def test_anything_unexpected_is_silence_and_writes_nothing(tmp_path, payload):
    d = _d(tmp_path)
    _ready(d)
    assert _hook(d, payload) == (0, "")
    assert os.listdir(d) == ["ready"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_a_directory_others_can_write_is_refused(tmp_path):
    d = _d(tmp_path, mode=0o777)
    _ready(d)
    assert _hook(d, _payload()) == (0, "")
    assert os.listdir(d) == ["ready"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_request_file_is_private(tmp_path):
    d = _d(tmp_path)
    _ready(d)
    seen = {}

    def peek(clock):
        p = os.path.join(d, "ab12cd34.req")
        if os.path.exists(p):
            seen["mode"] = os.stat(p).st_mode & 0o777
        _ready(d, clock.t)

    _hook(d, _payload(), _Clock(on_sleep=peek), wait_s=2.0)
    assert seen["mode"] == 0o600


def test_a_hook_that_dies_mid_wait_still_cleans_up(tmp_path):
    d = _d(tmp_path)
    _ready(d)

    def boom(clock):
        raise RuntimeError("killed")

    rc, out = _hook(d, _payload(), _Clock(on_sleep=boom))
    assert (rc, out) == (0, "")
    assert os.listdir(d) == ["ready"]


# --- the daemon --------------------------------------------------------------

class _Daemon:
    def __init__(self, d):
        self.sent = []
        self.t = [NOW]
        self.mono = [0.0]
        self.a = Approvals(self.sent.append, now=lambda: self.t[0],
                           mono=lambda: self.mono[0], dir_fn=lambda: d)

    def poll(self, usable=True, advance=1.0):
        self.mono[0] += advance
        self.a.poll(usable)


def _req(d, req_id="ab12cd34", tool="Bash", text="ls -la", t=NOW, ttl=30):
    with open(os.path.join(d, req_id + ".req"), "w") as f:
        json.dump({"id": req_id, "tool": tool, "text": text, "t": t,
                   "ttl": ttl}, f)


def test_a_request_is_offered_once_with_the_text_last(tmp_path):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    dm.poll()
    assert len(dm.sent) == 1
    msg = dm.sent[0]
    assert msg["t"] == "approve_req" and msg["id"] == "ab12cd34"
    assert msg["tool"] == "Bash" and msg["cmd"] == "ls -la"
    assert list(msg)[-1] == "cmd"
    assert 1 <= msg["ttl"] <= 30


def test_ready_is_touched_only_while_a_capable_board_is_connected(tmp_path):
    d = _d(tmp_path)
    dm = _Daemon(d)
    dm.poll(usable=True)
    # the file's mtime is the real clock; only the daemon's own time is faked
    assert approvals.daemon_ready(d, __import__("time").time())
    dm.poll(usable=False)
    assert not os.path.exists(os.path.join(d, "ready"))


def test_without_a_capable_board_nothing_is_offered(tmp_path):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll(usable=False)
    assert dm.sent == []


def test_feature_off_is_one_stat_and_nothing_else(tmp_path):
    dm = _Daemon(str(tmp_path / "does-not-exist"))
    dm.poll()
    assert dm.sent == []


def test_a_tap_becomes_a_res_file(tmp_path):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    dm.a.on_result({"t": "approve_res", "id": "ab12cd34", "ok": 1})
    assert open(os.path.join(d, "ab12cd34.res")).read() == "allow"
    # answered, so a later poll must not offer the still-present .req again
    dm.poll()
    assert len(dm.sent) == 1


@pytest.mark.parametrize("ok,word", [(1, "allow"), (0, "deny"), (2, "decline")])
def test_every_answer_maps(tmp_path, ok, word):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    dm.a.on_result({"id": "ab12cd34", "ok": ok})
    assert open(os.path.join(d, "ab12cd34.res")).read() == word


@pytest.mark.parametrize("msg", [
    {"id": "deadbeef", "ok": 1},          # nobody asked
    {"id": "ab12cd34", "ok": 7},          # not an answer
    {"id": "ab12cd34", "ok": "1"},
    {"id": "ab12cd34"},
    {"id": 5, "ok": 1},
    {"ok": 1},
])
def test_an_answer_nobody_asked_for_writes_nothing(tmp_path, msg):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    if msg.get("id") != "ab12cd34" or msg.get("ok") not in (0, 1, 2):
        dm.a.on_result(msg)
        assert not os.path.exists(os.path.join(d, "ab12cd34.res"))
        assert not os.path.exists(os.path.join(d, "deadbeef.res"))


def test_a_second_answer_for_the_same_request_is_ignored(tmp_path):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    dm.a.on_result({"id": "ab12cd34", "ok": 0})
    dm.a.on_result({"id": "ab12cd34", "ok": 1})
    assert open(os.path.join(d, "ab12cd34.res")).read() == "deny"


def test_a_request_the_hook_gave_up_on_is_withdrawn(tmp_path):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    os.unlink(os.path.join(d, "ab12cd34.req"))     # answered at the keyboard
    dm.poll()
    assert dm.sent[-1] == protocol.approve_end("ab12cd34")


def test_an_expired_request_is_withdrawn_and_swept(tmp_path):
    d = _d(tmp_path)
    _req(d, ttl=5)
    dm = _Daemon(d)
    dm.poll()
    dm.t[0] += 6
    dm.poll()
    assert dm.sent[-1]["t"] == "approve_end"
    dm.t[0] += 20
    dm.poll()
    assert not os.path.exists(os.path.join(d, "ab12cd34.req"))


def test_reconnect_offers_what_is_still_open_again(tmp_path):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    dm.a.resend()
    dm.poll()
    assert [m["t"] for m in dm.sent] == ["approve_req", "approve_req"]


def test_text_that_cannot_be_shown_is_declined_not_sent(tmp_path):
    d = _d(tmp_path)
    _req(d, text="echo café")        # a planted or hand-made request
    dm = _Daemon(d)
    dm.poll()
    assert dm.sent == []
    assert open(os.path.join(d, "ab12cd34.res")).read() == "decline"


def test_a_planted_request_for_an_unlisted_tool_is_declined(tmp_path):
    d = _d(tmp_path)
    _req(d, tool="mcp__evil__run")
    dm = _Daemon(d)
    dm.poll()
    assert dm.sent == []
    assert open(os.path.join(d, "ab12cd34.res")).read() == "decline"


def test_a_badly_named_file_is_ignored(tmp_path):
    d = _d(tmp_path)
    _req(d, req_id="../../etc")
    dm = _Daemon(d)
    dm.poll()
    assert dm.sent == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_daemon_refuses_a_directory_others_can_write(tmp_path):
    d = _d(tmp_path, mode=0o777)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    assert dm.sent == []


def test_the_command_text_never_reaches_the_log(tmp_path, capsys):
    d = _d(tmp_path)
    _req(d, text=f"curl -H 'Authorization: Bearer {SECRET}' https://x.test")
    dm = _Daemon(d)
    dm.poll()
    dm.a.on_result({"id": "ab12cd34", "ok": 1})
    err = capsys.readouterr().err
    assert "ab12cd34" in err and "Bash" in err
    assert SECRET not in err and "curl" not in err


def test_the_worst_case_message_fits_the_board_line(tmp_path):
    """180 characters, every one of them a quote or a backslash: JSON doubles
    each, which is the largest this can get. It must still fit 512 bytes, or a
    request the hook accepted would be silently dropped on the wire."""
    for ch in ('"', "\\"):
        msg = protocol.approve_req("ab12cd34", "NotebookEdit",
                                   ch * approvals.MAX_TEXT_CHARS, 120)
        raw, why = protocol.encode_checked(msg)
        assert why is None, (ch, len(protocol.encode(msg)))
        assert len(raw) < protocol.MAX_LINE_BYTES


# --- settings.json -----------------------------------------------------------

def _settings(tmp_path, data):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(data, indent=2) + "\n")
    return str(p)


BIN = "/home/u/.blink/bin/blink"


def test_enable_adds_one_entry_and_keeps_everyone_elses(tmp_path):
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
    p = _settings(tmp_path, {"hooks": {"PermissionRequest": [mine]},
                             "model": "m"})
    approvals.enable(p, BIN)
    approvals.enable(p, BIN)              # idempotent
    data = json.load(open(p))
    groups = data["hooks"]["PermissionRequest"]
    assert mine in groups and len(groups) == 2
    ours = [g for g in groups if g is not mine and g != mine][0]
    assert ours["matcher"] == approvals.TOOL_MATCHER
    assert ours["hooks"][0]["command"].endswith("approve-hook")
    assert "timeout" not in ours["hooks"][0]
    assert data["model"] == "m"
    assert approvals.enabled(p, BIN)
    assert os.path.isdir(approvals.approve_dir())


def test_disable_removes_only_ours_and_the_directory(tmp_path):
    mine = {"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}
    p = _settings(tmp_path, {"hooks": {"PermissionRequest": [mine]}})
    approvals.enable(p, BIN)
    approvals.disable(p, BIN)
    assert json.load(open(p))["hooks"]["PermissionRequest"] == [mine]
    assert not approvals.enabled(p, BIN)
    assert not os.path.exists(approvals.approve_dir())


def test_disable_on_a_clean_file_removes_the_empty_hooks_key(tmp_path):
    p = _settings(tmp_path, {"model": "m"})
    approvals.enable(p, BIN)
    approvals.disable(p, BIN)
    assert json.load(open(p)) == {"model": "m"}


def test_status_is_off_by_default(tmp_path):
    assert not approvals.enabled(_settings(tmp_path, {}), BIN)


# --- the bridge --------------------------------------------------------------

def test_the_bridge_offers_only_to_a_board_that_advertised_it(tmp_path):
    from pc.bridge import Bridge
    d = _d(tmp_path)
    _req(d)
    sent = []
    ap = Approvals(sent.append, now=lambda: NOW, mono=iter(
        [1.0, 2.0, 3.0, 4.0]).__next__, dir_fn=lambda: d)
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None,
               now=lambda: 0.0, approvals=ap)
    b.on_message({"t": "ping", "v": 2})        # alive, but silent about approve
    b.poll_approvals()
    assert sent == []
    b.on_message({"t": "pref", "v": 2, "provider": "claude", "approve": 1})
    b.poll_approvals()
    assert [m["t"] for m in sent] == ["approve_req"]
    b.on_message({"t": "approve_res", "v": 2, "id": "ab12cd34", "ok": 1})
    assert open(os.path.join(d, "ab12cd34.res")).read() == "allow"


def test_an_older_board_that_stops_saying_it_is_no_longer_offered(tmp_path):
    from pc.bridge import Bridge
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None,
               now=lambda: 0.0)
    b.on_message({"t": "pref", "v": 2, "provider": "claude", "approve": 1})
    assert b._board_approve
    b.on_message({"t": "pref", "v": 2, "provider": "claude"})
    assert not b._board_approve


def test_a_request_whose_tool_is_not_a_string_is_declined_not_raised(tmp_path):
    """The tool comes off disk. A list is unhashable, and raising here would
    escape poll() into the daemon's read loop."""
    d = _d(tmp_path)
    _req(d, tool=["Bash"])
    dm = _Daemon(d)
    dm.poll()
    assert dm.sent == []
    assert open(os.path.join(d, "ab12cd34.res")).read() == "decline"


def test_without_a_board_a_killed_hooks_request_is_still_swept(tmp_path):
    """The command text must not wait for a board to reconnect to be removed."""
    d = _d(tmp_path)
    _req(d, ttl=5)
    dm = _Daemon(d)
    dm.poll(usable=False)
    dm.t[0] += 600
    dm.poll(usable=False)
    assert not os.path.exists(os.path.join(d, "ab12cd34.req"))


def test_a_tap_after_the_hook_left_leaves_no_res_file(tmp_path):
    d = _d(tmp_path)
    _req(d)
    dm = _Daemon(d)
    dm.poll()
    os.remove(os.path.join(d, "ab12cd34.req"))     # the hook has exited
    with open(os.path.join(d, "ab12cd34.res"), "w") as f:
        f.write("allow")                            # a tap in the gap
    dm.poll()
    assert not os.path.exists(os.path.join(d, "ab12cd34.res"))
