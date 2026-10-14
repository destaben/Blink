"""Monthly spend against a fixed budget, read from `adi-aie user-cost --monthly`.

This account runs Claude Code through a company gateway, so neither the status
line nor Claude Desktop carries the 5-hour / 7-day plan windows the board is
built to show. What it does have is a monthly dollar figure and a fixed budget,
and that is a percentage like any other: spend / limit.

It is reported on the WEEKLY dial. The board's dials are labelled by the
firmware, not by the frame, so the caption still says the weekly name until the
firmware is relabelled.

The number is a best-effort estimate: the command itself warns that billing
can lag by up to 24 hours and may read 0.00. So the frame says exactly when the
figure was read, a failed read keeps the last good figure (it goes stale rather
than blank), and nothing here ever invents a value.

Configuration, all optional, read from the environment because the daemon is
started by launchd with almost none of the user's shell:

    BLINK_COST_LIMIT_USD   budget in dollars          (default 250)
    BLINK_COST_CMD         command to run, split like a shell
                           (default: adi-aie user-cost --monthly)
    BLINK_COST_REFRESH_S   seconds between reads      (default 600)
"""
import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

from pc.providers import base
from pc.providers.base import NormalizedUsageFrame, ProviderParser

PROVIDER_ID = "claude"
SRC_ID = "adi-cost"

DEFAULT_LIMIT_USD = 250.0
DEFAULT_REFRESH_S = 600.0
# The command talks to a billing API. Long enough for a slow gateway, short
# enough that a hung one cannot hold a worker thread for the life of the daemon.
COMMAND_TIMEOUT_S = 30.0
# A reading older than this is shown as stale rather than as current. Three
# missed refreshes at the default interval.
STALE_AFTER_S = 3 * 3600.0

# launchd starts the daemon with a bare PATH, so look where the tool lives on
# the machines this team uses as well as on PATH.
_EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin",
               os.path.expanduser("~/.local/bin"))

# "✔ $235.22 spent so far this month (2026-10)"
_SPENT = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+spent")


def parse_spend(text):
    """Dollars spent, as a float, from the command's output, or None."""
    if not isinstance(text, str):
        return None
    m = _SPENT.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def next_month_start(now_epoch):
    """Epoch of 00:00 local time on the first day of the next month."""
    now = datetime.datetime.fromtimestamp(now_epoch)
    if now.month == 12:
        first = datetime.datetime(now.year + 1, 1, 1)
    else:
        first = datetime.datetime(now.year, now.month + 1, 1)
    return first.timestamp()


def _env_float(name, default):
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if v > 0 else default


def _resolve_command():
    raw = os.environ.get("BLINK_COST_CMD")
    argv = shlex.split(raw) if raw else ["adi-aie", "user-cost", "--monthly"]
    if not argv:
        return None
    exe = shutil.which(argv[0], path=os.pathsep.join(
        [os.environ.get("PATH", "")] + list(_EXTRA_PATH)))
    if exe is None:
        return None
    return [exe] + argv[1:]


def _run(argv):
    """The command's stdout, or None on any failure. Never raises."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=COMMAND_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


class AdiCostProvider(ProviderParser):
    SRC = SRC_ID

    def __init__(self, now=time.time, runner=_run, resolver=_resolve_command):
        self._now = now
        self._runner = runner
        self._resolver = resolver
        self._limit = _env_float("BLINK_COST_LIMIT_USD", DEFAULT_LIMIT_USD)
        self._refresh_s = _env_float("BLINK_COST_REFRESH_S", DEFAULT_REFRESH_S)
        self._lock = threading.Lock()
        self._spend = None
        self._read_at = None
        self._next_try = 0.0
        self._busy = False
        self._complained = False

    def get_provider_id(self) -> str:
        return PROVIDER_ID

    def parse_output(self, text, now_epoch):
        """A frame from the command's output, or None."""
        spend = parse_spend(text)
        if spend is None:
            return None
        return self._frame(spend, now_epoch, now_epoch)

    def _frame(self, spend, read_at, now_epoch):
        pct = spend / self._limit * 100.0
        # base._pct-style range check: far past the budget means a misread,
        # and "--" is a better answer than a meter pinned to nonsense.
        if not (0 <= pct <= 1000):
            return None
        return NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=read_at,
            session_pct=base.UNKNOWN,
            weekly_pct=pct,
            weekly_resets_at=next_month_start(now_epoch),
            cost_dials=2,
            stale=(now_epoch - read_at) > STALE_AFTER_S,
        )

    def _refresh(self):
        try:
            argv = self._resolver()
            if argv is None:
                if not self._complained:
                    print("[adi-cost] adi-aie not found; ignoring this source",
                          file=sys.stderr)
                    self._complained = True
                return
            text = self._runner(argv)
            spend = parse_spend(text)
            if spend is None:
                if not self._complained:
                    print("[adi-cost] could not read a spend figure from the"
                          " command output; keeping the last reading",
                          file=sys.stderr)
                    self._complained = True
                return
            self._complained = False
            with self._lock:
                self._spend = spend
                self._read_at = self._now()
        finally:
            with self._lock:
                self._busy = False

    def poll(self, now_epoch):
        """The last good reading. Never blocks: the read runs on a thread."""
        with self._lock:
            start = (not self._busy) and now_epoch >= self._next_try
            if start:
                self._busy = True
                self._next_try = now_epoch + self._refresh_s
            spend, read_at = self._spend, self._read_at
        if start:
            threading.Thread(target=self._refresh, name="adi-cost",
                             daemon=True).start()
        if spend is None:
            return []
        frame = self._frame(spend, read_at, now_epoch)
        return [frame] if frame is not None else []
