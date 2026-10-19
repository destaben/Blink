"""Monthly spend against a fixed budget, read from `adi-aie user-cost --monthly`.

This account runs Claude Code through a company gateway, so neither the status
line nor Claude Desktop carries the 5-hour / 7-day plan windows the board is
built to show. What it does have is a monthly dollar figure and a fixed budget,
and that is a percentage like any other: spend / limit.

It is reported on the weekly slot. The board labels that slot MONTH for a cost
account (firmware/src/usage_view.c, cost_dials bit 2), not WEEKLY 7d.

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
import math
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
# the machines this team uses as well as on PATH. The per-user bin is added at
# call time (_extra_path): expanded here it would read the real home before any
# test's HOME redirect runs, and find a developer's own adi-aie.
_EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin")


def _extra_path():
    return _EXTRA_PATH + (os.path.join(os.path.expanduser("~"), ".local", "bin"),)

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
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    # A figure of hundreds of digits parses to inf, and int(round(inf)) in
    # _frame would raise OverflowError. Unreadable is the honest answer.
    return value if math.isfinite(value) else None


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


# Where Homebrew puts it on the machines this team uses. Checked before PATH
# because launchd starts the daemon with a bare one.
HOMEBREW_BIN = "/opt/homebrew/bin/adi-aie"


def _find_adi_aie():
    """Path of the adi-aie executable, or None: the Homebrew location first,
    then anywhere else it is installed."""
    if os.path.isfile(HOMEBREW_BIN) and os.access(HOMEBREW_BIN, os.X_OK):
        return HOMEBREW_BIN
    return shutil.which("adi-aie", path=os.pathsep.join(
        [os.environ.get("PATH", "")] + list(_extra_path())))


def _resolve_command():
    raw = os.environ.get("BLINK_COST_CMD")
    argv = shlex.split(raw) if raw else ["adi-aie", "user-cost", "--monthly"]
    if not argv:
        return None
    if raw:
        exe = shutil.which(argv[0], path=os.pathsep.join(
            [os.environ.get("PATH", "")] + list(_extra_path())))
    else:
        exe = _find_adi_aie()
    if exe is None:
        return None
    return [exe] + argv[1:]


def installed():
    """True when this machine runs Claude through adi-aie.

    This is the one switch between the two ways the board reads an account:
    adi-aie installed means spend against a budget (TODAY / MONTH), and not
    installed means the plan windows from Claude Desktop and the status line
    (SESSION 5h / WEEKLY 7d), exactly as before spend sources existed. It is
    checked on every cycle, so installing or removing the tool takes effect
    without restarting the daemon.
    """
    return _resolve_command() is not None


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
        # A negative or unreadable figure is a misread, and "--" is a better
        # answer than a meter pinned to nonsense. Past the ceiling is not a
        # misread: it is real spend, so it is clamped to the board's maximum
        # and the meter still reads over budget (dropping it went blank).
        if not (pct >= 0):
            return None
        pct = min(pct, base.PCT_CEILING)
        return NormalizedUsageFrame(
            provider=PROVIDER_ID, src=SRC_ID, observed_at=read_at,
            session_pct=base.UNKNOWN,
            weekly_pct=pct,
            weekly_resets_at=next_month_start(now_epoch),
            cost_dials=2,
            # Past the budget the percentage only says how far. The budget is
            # raised on request and nothing readable carries it, so once it
            # is crossed the board is also told the dollars themselves.
            weekly_usd=int(round(spend)) if spend >= self._limit else 0,
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
        """The last good reading. Never blocks: the read runs on a thread.

        Nothing at all, and no command run, when adi-aie is not installed: the
        account is then a plan account and this source has no business on it.
        """
        if self._resolver() is None:
            return []
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
