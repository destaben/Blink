"""Monthly spend as a dial: refuse to show a number the command did not give."""
import datetime

from pc.providers import adi_cost
from pc.providers.adi_cost import AdiCostProvider, next_month_start, parse_spend

OUT = ("\n✔ $235.22 spent so far this month (2026-10)\n"
       "  david.estaben@adidas.com · monthly billing period\n"
       "⚠ Best-effort estimate for this month — billing can lag up to 24h,"
       " so this may be incomplete (even $0.00).\n")

NOW = datetime.datetime(2026, 10, 14, 17, 40).timestamp()


def _provider(runner, now=lambda: NOW):
    return AdiCostProvider(now=now, runner=runner,
                           resolver=lambda: ["adi-aie", "user-cost"])


def test_parses_the_real_output():
    assert parse_spend(OUT) == 235.22


def test_parses_thousands_separator_and_zero():
    assert parse_spend("✔ $1,234.50 spent so far") == 1234.5
    assert parse_spend("✔ $0.00 spent so far") == 0.0


def test_garbage_is_none_not_zero():
    assert parse_spend("") is None
    assert parse_spend(None) is None
    assert parse_spend("error: not logged in") is None
    # The estimate warning also contains "$0.00"; it must not be read as spend.
    assert parse_spend("⚠ may be incomplete (even $0.00).") is None


def test_frame_percentage_against_the_default_limit():
    f = _provider(lambda argv: OUT).parse_output(OUT, NOW)
    assert f.provider == "claude"
    assert f.src == "adi-cost"
    assert round(f.weekly_pct, 2) == 94.09
    assert f.session_pct == -1.0
    assert f.has_usage()


def test_reset_is_the_first_of_next_month():
    reset = datetime.datetime.fromtimestamp(next_month_start(NOW))
    assert (reset.year, reset.month, reset.day, reset.hour) == (2026, 11, 1, 0)
    dec = datetime.datetime(2026, 12, 20).timestamp()
    reset = datetime.datetime.fromtimestamp(next_month_start(dec))
    assert (reset.year, reset.month, reset.day) == (2027, 1, 1)


def test_over_budget_is_a_real_reading_far_over_is_not():
    p = _provider(lambda argv: OUT)
    assert p.parse_output("✔ $260.00 spent so far", NOW).weekly_pct > 100
    assert p.parse_output("✔ $999999.00 spent so far", NOW) is None


def test_poll_returns_the_last_good_reading_after_a_failure():
    calls = []

    def runner(argv):
        calls.append(argv)
        return OUT if len(calls) == 1 else None

    p = _provider(runner)
    p._refresh()
    assert p.poll(NOW)[0].weekly_pct > 94
    p._refresh()  # the command now fails
    f = p.poll(NOW + 60)[0]
    assert round(f.weekly_pct, 2) == 94.09


def test_a_reading_goes_stale_instead_of_blank():
    p = _provider(lambda argv: OUT)
    p._refresh()
    late = NOW + adi_cost.STALE_AFTER_S + 1
    assert p.poll(late)[0].stale is True


def test_no_reading_yet_means_no_frame():
    p = _provider(lambda argv: None)
    p._refresh()
    assert p.poll(NOW) == []


def test_missing_command_is_silent_not_fatal():
    p = AdiCostProvider(now=lambda: NOW, runner=lambda a: OUT,
                        resolver=lambda: None)
    p._refresh()
    assert p.poll(NOW) == []


# --- how it coexists with the sources that carry real plan windows ---------

from pc.ingest import IngestionBus
from pc.providers import base


class _Fixed(base.ProviderParser):
    def __init__(self, frames):
        self._frames = frames

    def get_provider_id(self):
        return "claude"

    def poll(self, now_epoch):
        return list(self._frames)


def _plan_frame(weekly, stale=False):
    return base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW, session_pct=10.0,
        weekly_pct=weekly, weekly_resets_at=NOW + 86400, stale=stale)


def _cost_provider():
    p = _provider(lambda argv: OUT)
    p._refresh()
    return p


def _bus(*providers):
    return IngestionBus(providers=list(providers), now=lambda: NOW)


def test_plan_windows_win_over_spend_when_both_exist():
    bus = _bus(_Fixed([_plan_frame(42.0)]), _cost_provider())
    assert [f.src for f in bus.poll_frames()] == ["cli"]


def test_stale_plan_window_does_not_block_the_spend():
    bus = _bus(_Fixed([_plan_frame(42.0, stale=True)]), _cost_provider())
    assert sorted(f.src for f in bus.poll_frames()) == ["adi-cost", "cli"]


def test_spend_fills_the_weekly_dial_when_no_plan_window_exists():
    bus = _bus(_Fixed([]), _cost_provider())
    assert [f.src for f in bus.poll_frames()] == ["adi-cost"]


def test_spend_never_teaches_the_weekly_anchor(monkeypatch):
    seen = []
    monkeypatch.setattr("pc.ingest.weekly_anchor.observe",
                        lambda frames, path, now: seen.extend(frames))
    _bus(_Fixed([]), _cost_provider()).poll_frames()
    assert all(f.src != "adi-cost" for f in seen)
