"""Monthly spend as a dial: refuse to show a number the command did not give."""
import datetime
import json
import os

from pc.providers import adi_cost, base
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
    assert round(f.weekly_pct, 2) == 94.09  # 235.22 / 250
    assert f.weekly_usd == 0  # under budget: the percentage is the answer
    assert f.session_pct == -1.0
    assert f.has_usage()


def test_past_the_budget_the_frame_also_carries_the_dollars():
    over = "\u2714 $531.40 spent so far this month (2026-10)\n"
    f = _provider(lambda argv: over).parse_output(over, NOW)
    assert round(f.weekly_pct, 1) == 212.6  # 531.40 / 250
    assert f.weekly_usd == 531


def test_exactly_at_the_budget_counts_as_past_it():
    at = "\u2714 $250.00 spent so far this month (2026-10)\n"
    f = _provider(lambda argv: at).parse_output(at, NOW)
    assert f.weekly_pct == 100.0 and f.weekly_usd == 250


def test_the_budget_can_be_raised_by_environment(monkeypatch):
    monkeypatch.setenv("BLINK_COST_LIMIT_USD", "750")
    over = "\u2714 $531.40 spent so far this month (2026-10)\n"
    f = _provider(lambda argv: over).parse_output(over, NOW)
    assert round(f.weekly_pct, 1) == 70.9 and f.weekly_usd == 0


def test_reset_is_the_first_of_next_month():
    reset = datetime.datetime.fromtimestamp(next_month_start(NOW))
    assert (reset.year, reset.month, reset.day, reset.hour) == (2026, 11, 1, 0)
    dec = datetime.datetime(2026, 12, 20).timestamp()
    reset = datetime.datetime.fromtimestamp(next_month_start(dec))
    assert (reset.year, reset.month, reset.day) == (2027, 1, 1)


def test_over_budget_is_a_real_reading_however_far_over():
    # Far past the budget is real spend and must still read as over budget:
    # it is clamped to the board's ceiling, not dropped (a dropped frame sent
    # a spend account at ten times its budget to a blank dial, 2026-10-19).
    p = _provider(lambda argv: OUT)
    assert p.parse_output("✔ $260.00 spent so far", NOW).weekly_pct > 100
    far = p.parse_output("✔ $999999.00 spent so far", NOW)
    assert far.weekly_pct == base.PCT_CEILING
    assert far.weekly_usd == 999999


def test_a_figure_that_overflows_a_float_is_unreadable_not_infinite():
    # 400 digits parse to inf, and the frame's int(round(spend)) would raise.
    huge = "✔ $" + "9" * 400 + ".00 spent so far"
    assert parse_spend(huge) is None
    assert _provider(lambda argv: huge).parse_output(huge, NOW) is None


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


def test_each_spend_source_marks_only_its_own_dial(tmp_path):
    from pc.providers.session_cost import SessionCostProvider
    assert _provider(lambda argv: OUT).parse_output(OUT, NOW).cost_dials == 2
    d = tmp_path / "-p"
    d.mkdir()
    f = d / "s.jsonl"
    f.write_text(json.dumps({"message": {"id": "a", "role": "assistant",
                 "usage": {"output_tokens": 1_000_000}}}) + "\n")
    os.utime(f, (NOW, NOW))
    sp = SessionCostProvider(now=lambda: NOW, root=str(tmp_path),
                             installed=lambda: True)
    sp._refresh()
    assert sp.poll(NOW)[0].cost_dials == 1


def test_merged_frame_marks_exactly_the_spend_dials():
    from pc import normalizer
    plan = _plan_frame(42.0)
    spend_w = _provider(lambda argv: OUT).parse_output(OUT, NOW)
    # a plan session window plus a spend weekly window: only the weekly dial
    # is spend
    merged = normalizer.merge([plan, spend_w])
    assert merged.cost_dials == 2
    # nothing but plan frames: plan account, nothing marked
    assert normalizer.merge([plan]).cost_dials == 0


def test_merge_carries_the_dollars_to_the_wire():
    from pc import normalizer, protocol
    over = "✔ $531.40 spent so far this month (2026-10)\n"
    merged = normalizer.merge([_plan_frame(42.0),
                               _provider(lambda argv: over).parse_output(over, NOW)])
    assert merged.weekly_usd == 531
    assert protocol.frame_to_usage(merged, NOW)["usd"] == 531
    # A plan account never sends it.
    assert normalizer.merge([_plan_frame(42.0)]).weekly_usd == 0


# --- adi-aie installed or not: the switch between spend and plan ----------

def _transcript_root(tmp_path):
    d = tmp_path / "-p"
    d.mkdir()
    f = d / "s.jsonl"
    f.write_text(json.dumps({"message": {"id": "a", "role": "assistant",
                 "usage": {"output_tokens": 1_000_000}}}) + "\n")
    os.utime(f, (NOW, NOW))
    return str(tmp_path)


def test_not_installed_means_no_spend_frames_and_no_command_run(tmp_path):
    """Plan account: neither fallback offers anything, and nothing is run."""
    from pc.providers.session_cost import SessionCostProvider
    ran = []
    p = AdiCostProvider(now=lambda: NOW,
                        runner=lambda a: ran.append(a) or OUT,
                        resolver=lambda: None)
    assert p.poll(NOW) == []
    p._thread_wait = None
    sp = SessionCostProvider(now=lambda: NOW, root=_transcript_root(tmp_path),
                             installed=lambda: False)
    assert sp.poll(NOW) == []
    assert sp._reader == {} and sp._cost is None     # no transcript was read
    assert ran == []


def test_installed_session_cost_offers_its_frame(tmp_path):
    from pc.providers.session_cost import SessionCostProvider
    sp = SessionCostProvider(now=lambda: NOW, root=_transcript_root(tmp_path),
                             installed=lambda: True)
    sp._refresh()
    assert sp.poll(NOW)[0].cost_dials == 1


def test_homebrew_location_wins_then_anywhere_on_path(tmp_path, monkeypatch):
    exe = tmp_path / "adi-aie"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.delenv("BLINK_COST_CMD", raising=False)
    # not at the Homebrew path, not on PATH: not installed
    monkeypatch.setattr(adi_cost, "HOMEBREW_BIN", str(tmp_path / "nope"))
    monkeypatch.setattr(adi_cost, "_EXTRA_PATH", ())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert adi_cost.installed() is False
    # found globally
    monkeypatch.setenv("PATH", str(tmp_path))
    assert adi_cost.installed() is True
    assert adi_cost._resolve_command()[0] == str(exe)
    # the Homebrew path is preferred when present
    monkeypatch.setattr(adi_cost, "HOMEBREW_BIN", str(exe))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert adi_cost._resolve_command()[0] == str(exe)


def test_non_executable_homebrew_file_does_not_count(tmp_path, monkeypatch):
    exe = tmp_path / "adi-aie"
    exe.write_text("")
    exe.chmod(0o644)
    monkeypatch.delenv("BLINK_COST_CMD", raising=False)
    monkeypatch.setattr(adi_cost, "HOMEBREW_BIN", str(exe))
    monkeypatch.setattr(adi_cost, "_EXTRA_PATH", ())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert adi_cost.installed() is False
