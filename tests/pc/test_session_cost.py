"""The left dial is today's spend, an estimate; these pin what it may claim."""
import datetime
import json
import os

from pc.ingest import IngestionBus
from pc.providers import base
from pc.providers.session_cost import (DEFAULT_RATES, SessionCostProvider,
                                       _Transcript, day_start, next_day_start,
                                       usage_cost)

# Noon, local time, on an arbitrary day. Anchored to the local day so the
# tests mean the same thing in every time zone: "earlier today" is NOW - 3h
# and "yesterday" is NOW - 1 day wherever the runner happens to be.
NOW = day_start(1_787_700_000.0) + 12 * 3600.0
DAY = 86400.0


def _iso(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _msg(mid, inp=0, out=0, cread=0, cwrite=0, role="assistant", at=None):
    d = {"uuid": "u-" + mid, "message": {
        "id": mid, "role": role,
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_read_input_tokens": cread,
                  "cache_creation_input_tokens": cwrite}}}
    if at is not None:
        d["timestamp"] = _iso(at)
    return json.dumps(d) + "\n"


def _project(tmp_path, lines, mtime=NOW, name="-proj"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    f = d / "s1.jsonl"
    f.write_text("".join(lines))
    os.utime(f, (mtime, mtime))
    return f


def test_usage_cost_uses_every_token_kind():
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
         "cache_read_input_tokens": 1_000_000,
         "cache_creation_input_tokens": 1_000_000}
    assert usage_cost(u, (1, 2, 3, 4)) == 10.0


def test_usage_cost_ignores_missing_and_non_numeric_fields():
    assert usage_cost({}, DEFAULT_RATES) == 0.0
    assert usage_cost(None, DEFAULT_RATES) == 0.0
    assert usage_cost({"input_tokens": "lots", "output_tokens": True},
                      DEFAULT_RATES) == 0.0


def test_default_rates_track_the_two_status_line_readings():
    # Cumulative tokens (millions) at the moments a real status line read
    # "ctx 18% $2.79" and "ctx 24% $5.90". An estimate, so a tolerance.
    for out, cread, cwrite, real in ((0.057, 8.4, 0.183, 2.79),
                                     (0.094, 18.3, 0.242, 5.90)):
        est = usage_cost({"output_tokens": out * 1e6,
                          "cache_read_input_tokens": cread * 1e6,
                          "cache_creation_input_tokens": cwrite * 1e6},
                         DEFAULT_RATES)
        assert abs(est - real) / real < 0.15, (est, real)


def test_a_message_streamed_twice_counts_once_with_its_last_write(tmp_path):
    f = _project(tmp_path, [_msg("a", out=10), _msg("a", out=1_000_000)])
    t = _Transcript()
    t.advance(f)
    assert round(t.cost((0, 15.0, 0, 0)), 2) == 15.0


def test_a_half_written_line_is_not_consumed(tmp_path):
    f = _project(tmp_path, [_msg("a", out=1_000_000)])
    with open(f, "a") as fh:
        fh.write('{"message": {"id": "b", "role": "assis')
    t = _Transcript()
    t.advance(f)
    assert len(t.usage) == 1
    with open(f, "a") as fh:  # the writer finishes the line
        fh.write('tant", "usage": {"output_tokens": 5}}}\n')
    t.advance(f)
    assert len(t.usage) == 2


def test_only_new_bytes_are_read_on_the_next_look(tmp_path):
    f = _project(tmp_path, [_msg("a", out=1)])
    t = _Transcript()
    t.advance(f)
    first = t.offset
    with open(f, "a") as fh:
        fh.write(_msg("b", out=1))
    t.advance(f)
    assert t.offset > first and len(t.usage) == 2


def test_user_messages_and_garbage_lines_cost_nothing(tmp_path):
    f = _project(tmp_path, [_msg("u", out=999, role="user"), "not json\n"])
    t = _Transcript()
    t.advance(f)
    assert t.cost(DEFAULT_RATES) == 0.0


def _provider(tmp_path):
    return SessionCostProvider(now=lambda: NOW, root=str(tmp_path),
                               installed=lambda: True)


def test_dial_is_todays_cost_over_the_daily_limit(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)])  # $15 at the default rate
    p = _provider(tmp_path)
    p._refresh()
    f = p.poll(NOW)[0]
    assert (f.provider, f.src) == ("claude", "session-cost")
    assert round(f.session_pct, 2) == 50.0  # 15 / 30
    assert f.weekly_pct == base.UNKNOWN
    assert f.cost_dials == 1


def test_far_over_the_daily_limit_the_dial_reads_full_not_blank(tmp_path):
    # $375 against the default $30 is twelve times over. Dropping the frame
    # here blanked the dial exactly when the spend was worst (2026-10-19).
    _project(tmp_path, [_msg("a", out=25_000_000)])
    p = _provider(tmp_path)
    p._refresh()
    frames = p.poll(NOW)
    assert len(frames) == 1
    assert frames[0].session_pct == base.PCT_CEILING


def test_the_countdown_runs_to_local_midnight(tmp_path):
    _project(tmp_path, [_msg("a", out=1)])
    p = _provider(tmp_path)
    p._refresh()
    f = p.poll(NOW)[0]
    assert f.session_resets_at == next_day_start(NOW)
    assert 0 < f.session_resets_at - NOW <= 12.5 * 3600


def test_every_session_of_the_day_adds_up(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)], mtime=NOW - 600)
    _project(tmp_path, [_msg("b", out=500_000)], mtime=NOW - 10, name="-other")
    p = _provider(tmp_path)
    p._refresh()
    assert round(p.poll(NOW)[0].session_pct, 2) == 75.0  # (15 + 7.5) / 30


def test_a_session_that_went_quiet_hours_ago_still_counts(tmp_path):
    # The limit is spent whether or not anybody is typing.
    _project(tmp_path, [_msg("a", out=1_000_000)], mtime=NOW - 3 * 3600)
    p = _provider(tmp_path)
    p._refresh()
    assert round(p.poll(NOW)[0].session_pct, 2) == 50.0


def test_yesterdays_half_of_a_session_left_open_does_not_count(tmp_path):
    _project(tmp_path, [_msg("old", out=2_000_000, at=NOW - DAY),
                        _msg("new", out=1_000_000, at=NOW - 60)])
    p = _provider(tmp_path)
    p._refresh()
    assert round(p.poll(NOW)[0].session_pct, 2) == 50.0  # only the $15


def test_a_file_untouched_since_yesterday_is_not_opened(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)], mtime=NOW - DAY)
    p = _provider(tmp_path)
    p._refresh()
    assert p.poll(NOW)[0].session_pct == 0.0
    assert p._reader == {}


def test_the_day_is_rebuilt_from_the_files_after_a_restart(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)])
    first, second = _provider(tmp_path), _provider(tmp_path)
    first._refresh()
    second._refresh()
    assert first.poll(NOW)[0].session_pct == second.poll(NOW)[0].session_pct


def test_after_midnight_yesterdays_figure_is_not_shown(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)])
    p = _provider(tmp_path)
    p._refresh()
    after = next_day_start(NOW) + 1
    p._next_try = after + 100  # keep poll() from starting a refresh
    assert p.poll(after) == []


def test_the_daily_limit_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("BLINK_DAILY_LIMIT_USD", "60")
    _project(tmp_path, [_msg("a", out=1_000_000)])
    p = _provider(tmp_path)
    p._refresh()
    assert round(p.poll(NOW)[0].session_pct, 2) == 25.0


def test_no_projects_directory_is_silent(tmp_path):
    p = _provider(tmp_path / "missing")
    p._refresh()
    assert p.poll(NOW) == []


# --- priority against the sources that carry real plan windows -------------

class _Fixed(base.ProviderParser):
    def __init__(self, frames):
        self._frames = frames

    def get_provider_id(self):
        return "claude"

    def poll(self, now_epoch):
        return list(self._frames)


def _plan(session, stale=False):
    return base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW, session_pct=session,
        stale=stale)


def _bus(*providers):
    return IngestionBus(providers=list(providers), now=lambda: NOW)


def test_spend_fills_the_session_dial_when_no_plan_window_exists(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)])
    p = _provider(tmp_path)
    p._refresh()
    assert [f.src for f in _bus(_Fixed([]), p).poll_frames()] == ["session-cost"]


def test_plan_session_window_wins_over_session_spend(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)])
    p = _provider(tmp_path)
    p._refresh()
    frames = _bus(_Fixed([_plan(40.0)]), p).poll_frames()
    assert [f.src for f in frames] == ["cli"]


def test_a_weekly_only_plan_frame_does_not_block_session_spend(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)])
    p = _provider(tmp_path)
    p._refresh()
    weekly_only = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW, weekly_pct=50.0)
    srcs = sorted(f.src for f in _bus(_Fixed([weekly_only]), p).poll_frames())
    assert srcs == ["cli", "session-cost"]
