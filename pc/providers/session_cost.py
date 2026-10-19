"""Today's Claude Code spend, as a share of the daily dollar limit.

The company gateway caps spend at a fixed amount per day, and that cap is used
up by every session of the day together, not by the one in front of you. So the
left dial counts the whole day: the cost of every assistant message written
today, across all the open and closed sessions, over the limit. It starts the
day at 0 and only climbs until local midnight, which is also when its countdown
ends.

Claude Code hands the cost only to the status line command. On an account where
that command is managed by the company, nothing else sees it, so this provider
rebuilds an ESTIMATE from the transcripts: every assistant message in
~/.claude/projects/<project>/<session>.jsonl carries the tokens it used and a
timestamp, and the sum of tokens x price over today's messages is the cost.
Nothing is kept between runs: on a restart the day is rebuilt from the files,
so the count survives the daemon.

It is an estimate and says so: the prices are Claude Code's own table, which
is not published, so the defaults below were fitted against two status-line
readings from a real session (ctx 18% -> $2.79, ctx 24% -> $5.90) and land
within roughly 10% of them. Override with BLINK_SESSION_RATES when the real
prices are known. It also only sees this machine, and subagent transcripts are
not summed, so a day that leans on subagents or on another machine reads low.
`adi-aie user-cost --daily` is the billed figure, but billing can lag by up to
24 hours, which is useless for watching a day fill up.

A message's own timestamp decides which day it belongs to, not the file's:
a session left open past midnight keeps writing to the same file, and yester-
day's half of it must not count against today. A message with no timestamp
counts as today when its file was written today.

Configuration, all optional (the daemon runs under launchd with no shell env):

    BLINK_DAILY_LIMIT_USD   daily limit in dollars            (default 30)
    BLINK_SESSION_RATES     "input,output,cache_read,cache_write" in USD per
                            million tokens        (default 3,15,0.18,3.75)
    BLINK_CLAUDE_PROJECTS   transcripts root (default ~/.claude/projects)
"""
import datetime
import glob
import json
import os
import sys
import threading
import time

from pc.providers import adi_cost, base
from pc.providers.base import NormalizedUsageFrame, ProviderParser

PROVIDER_ID = "claude"
SRC_ID = "session-cost"

DEFAULT_LIMIT_USD = 30.0
DEFAULT_RATES = (3.0, 15.0, 0.18, 3.75)
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


def day_start(now_epoch):
    """Epoch of 00:00 local time on the day containing `now_epoch`."""
    d = datetime.datetime.fromtimestamp(now_epoch)
    return datetime.datetime(d.year, d.month, d.day).timestamp()


def next_day_start(now_epoch):
    """Epoch of 00:00 local time on the next day.

    Added to the calendar date rather than as 86400 s, so a day with a clock
    change still ends at midnight.
    """
    d = datetime.datetime.fromtimestamp(now_epoch)
    return (datetime.datetime(d.year, d.month, d.day)
            + datetime.timedelta(days=1)).timestamp()


def _epoch(stamp):
    """Epoch of a transcript's ISO-8601 `timestamp`, or None."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.datetime.fromisoformat(
            stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


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
        # message id -> (epoch or None, usage). The transcript logs a message
        # more than once while it streams, and the last write is the complete
        # one.
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
                # The id comes out of a transcript the user may not have
                # written, and a list or dict here is unhashable. That raises
                # TypeError, which _refresh does not catch, so the session
                # dial would freeze. Only a string is a usable key.
                key = m.get("id") or d.get("uuid")
                if not isinstance(key, str):
                    key = len(self.usage)
                self.usage[key] = (_epoch(d.get("timestamp")), u)
        self.offset += end

    def cost(self, rates, since=0.0):
        """Dollars for the messages written at or after `since`."""
        return sum(usage_cost(u, rates) for t, u in self.usage.values()
                   if t is None or t >= since)


class SessionCostProvider(ProviderParser):
    SRC = SRC_ID

    def __init__(self, now=time.time, root=None, installed=adi_cost.installed):
        self._now = now
        self._installed = installed
        self._root = root or os.environ.get("BLINK_CLAUDE_PROJECTS") or \
            os.path.expanduser("~/.claude/projects")
        self._limit = _env_float("BLINK_DAILY_LIMIT_USD", DEFAULT_LIMIT_USD)
        self._rates = _rates()
        self._lock = threading.Lock()
        self._reader = {}      # path -> _Transcript
        self._cost = None
        self._day = None       # the day_start the cost was computed for
        self._read_at = None
        self._next_try = 0.0
        self._busy = False
        self._complained = False

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def _refresh(self):
        try:
            now = self._now()
            start = day_start(now)
            paths = glob.glob(os.path.join(self._root, "*", "*.jsonl"))
            if not paths:
                with self._lock:
                    self._cost = None
                return
            total = 0.0
            live = set()
            for p in paths:
                try:
                    # A file untouched since before midnight cannot hold a
                    # message from today, so it is never even opened.
                    if os.path.getmtime(p) < start:
                        continue
                    reader = self._reader.setdefault(p, _Transcript())
                    reader.advance(p)
                except OSError as e:
                    if not self._complained:
                        print(f"[session-cost] cannot read transcript: {e}",
                              file=sys.stderr)
                        self._complained = True
                    continue
                live.add(p)
                total += reader.cost(self._rates, start)
            # Yesterday's files are not needed again; do not hold them.
            for p in [p for p in self._reader if p not in live]:
                del self._reader[p]
            with self._lock:
                self._cost, self._day, self._read_at = total, start, now
        finally:
            with self._lock:
                self._busy = False

    def poll(self, now_epoch):
        """The last estimate. Never blocks: the read runs on a thread.

        Nothing, and no transcript read, when adi-aie is not installed: on a
        plan account the session dial is the real 5 h window, not a cost.
        """
        if not self._installed():
            return []
        with self._lock:
            start = (not self._busy) and now_epoch >= self._next_try
            if start:
                self._busy = True
                self._next_try = now_epoch + REFRESH_S
            cost, day, read_at = self._cost, self._day, self._read_at
        if start:
            threading.Thread(target=self._refresh, name="session-cost",
                             daemon=True).start()
        if cost is None or day is None:
            return []
        # Past midnight the figure still describes yesterday until the next
        # refresh lands a few seconds later; showing it against today's
        # countdown would be a confident wrong number.
        if day != day_start(now_epoch):
            return []
        pct = cost / self._limit * 100.0
        if not (pct >= 0):
            return []
        pct = min(pct, base.PCT_CEILING)
        return [NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=read_at,
            session_pct=pct, session_resets_at=next_day_start(now_epoch),
            weekly_pct=base.UNKNOWN, cost_dials=1)]
