"""Cost of the current Claude Code session, as a share of the monthly budget.

Claude Code hands the session cost only to the status line command. On an
account where that command is managed by the company, nothing else sees it, so
this provider rebuilds an ESTIMATE from the session's own transcript: every
assistant message in ~/.claude/projects/<project>/<session>.jsonl carries the
tokens it used, and the sum of tokens x price is the cost.

It is an estimate and says so: the prices are Claude Code's own table, which
is not published, so the defaults below were fitted against two status-line
readings from a real session (ctx 18% -> $2.79, ctx 24% -> $5.90) and land
within roughly 10% of them. Override with BLINK_SESSION_RATES when the real
prices are known. Subagent transcripts are not summed, so a session that
leans on subagents reads low.

The dial is the session's cost divided by the monthly budget, the same budget
the monthly dial uses, so "2%" on the session dial means this session has used
2% of the month's allowance.

"Current session" is the transcript written to most recently. With several
sessions open it follows whichever spoke last, which is also the one a person
is looking at.

Configuration, all optional (the daemon runs under launchd with no shell env):

    BLINK_COST_LIMIT_USD    budget in dollars                  (default 250)
    BLINK_SESSION_RATES     "input,output,cache_read,cache_write" in USD per
                            million tokens        (default 3,15,0.18,3.75)
    BLINK_CLAUDE_PROJECTS   transcripts root (default ~/.claude/projects)
"""
import glob
import json
import os
import sys
import threading
import time

from pc.providers import base
from pc.providers.base import NormalizedUsageFrame, ProviderParser

PROVIDER_ID = "claude"
SRC_ID = "session-cost"

DEFAULT_LIMIT_USD = 250.0
DEFAULT_RATES = (3.0, 15.0, 0.18, 3.75)
# A transcript untouched for this long is not "the current session".
ACTIVE_WINDOW_S = 30 * 60.0
REFRESH_S = 5.0


def _env_float(name, default):
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if v > 0 else default


def _rates():
    raw = os.environ.get("BLINK_SESSION_RATES", "")
    try:
        parts = tuple(float(x) for x in raw.split(","))
    except ValueError:
        return DEFAULT_RATES
    return parts if len(parts) == 4 and all(p >= 0 for p in parts) \
        else DEFAULT_RATES


def usage_cost(usage, rates):
    """Dollars for one message's `usage` object. Missing fields count as 0."""
    if not isinstance(usage, dict):
        return 0.0

    def n(key):
        v = usage.get(key, 0)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) \
            else 0

    inp, out, cread, cwrite = rates
    return (n("input_tokens") * inp + n("output_tokens") * out
            + n("cache_read_input_tokens") * cread
            + n("cache_creation_input_tokens") * cwrite) / 1e6


class _Transcript:
    """Incremental reader: only the bytes appended since the last look."""

    def __init__(self):
        self.offset = 0
        self.size = 0
        # message id -> usage. The transcript logs a message more than once
        # while it streams, and the last write is the complete one.
        self.usage = {}

    def advance(self, path):
        size = os.path.getsize(path)
        if size < self.offset:  # rewritten or rotated: start over
            self.offset, self.usage = 0, {}
        with open(path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        end = chunk.rfind(b"\n") + 1  # never consume a half-written line
        for line in chunk[:end].splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") if isinstance(d, dict) else None
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            u = m.get("usage")
            if isinstance(u, dict):
                self.usage[m.get("id") or d.get("uuid") or len(self.usage)] = u
        self.offset += end

    def cost(self, rates):
        return sum(usage_cost(u, rates) for u in self.usage.values())


class SessionCostProvider(ProviderParser):
    SRC = SRC_ID

    def __init__(self, now=time.time, root=None):
        self._now = now
        self._root = root or os.environ.get("BLINK_CLAUDE_PROJECTS") or \
            os.path.expanduser("~/.claude/projects")
        self._limit = _env_float("BLINK_COST_LIMIT_USD", DEFAULT_LIMIT_USD)
        self._rates = _rates()
        self._lock = threading.Lock()
        self._reader = {}      # path -> _Transcript
        self._cost = None
        self._mtime = None
        self._next_try = 0.0
        self._busy = False

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def _newest(self):
        best, best_m = None, -1.0
        for p in glob.glob(os.path.join(self._root, "*", "*.jsonl")):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if m > best_m:
                best, best_m = p, m
        return best, best_m

    def _refresh(self):
        try:
            path, mtime = self._newest()
            if path is None:
                with self._lock:
                    self._cost = None
                return
            reader = self._reader.setdefault(path, _Transcript())
            reader.advance(path)
            with self._lock:
                self._cost = reader.cost(self._rates)
                self._mtime = mtime
        except OSError as e:
            print(f"[session-cost] cannot read transcript: {e}",
                  file=sys.stderr)
        finally:
            with self._lock:
                self._busy = False

    def poll(self, now_epoch):
        """The last estimate. Never blocks: the read runs on a thread."""
        with self._lock:
            start = (not self._busy) and now_epoch >= self._next_try
            if start:
                self._busy = True
                self._next_try = now_epoch + REFRESH_S
            cost, mtime = self._cost, self._mtime
        if start:
            threading.Thread(target=self._refresh, name="session-cost",
                             daemon=True).start()
        if cost is None or mtime is None:
            return []
        if now_epoch - mtime > ACTIVE_WINDOW_S:
            return []
        pct = cost / self._limit * 100.0
        if not (0 <= pct <= 1000):
            return []
        return [NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=mtime,
            session_pct=pct, weekly_pct=base.UNKNOWN, cost_dials=1)]
