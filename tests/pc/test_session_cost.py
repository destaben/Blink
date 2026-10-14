"""The session dial is an estimate; these pin what it may and may not claim."""
import json
import os

from pc.ingest import IngestionBus
from pc.providers import base
from pc.providers.session_cost import (DEFAULT_RATES, SessionCostProvider,
                                       _Transcript, usage_cost)

NOW = 1_787_700_000.0


def _msg(mid, inp=0, out=0, cread=0, cwrite=0, role="assistant"):
    return json.dumps({"uuid": "u-" + mid, "message": {
        "id": mid, "role": role,
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_read_input_tokens": cread,
                  "cache_creation_input_tokens": cwrite}}}) + "\n"


def _project(tmp_path, lines, mtime=NOW):
    d = tmp_path / "-proj"
    d.mkdir()
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
    return SessionCostProvider(now=lambda: NOW, root=str(tmp_path))


def test_dial_is_session_cost_over_the_budget(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)])  # $15 at the default rate
    p = _provider(tmp_path)
    p._refresh()
    f = p.poll(NOW)[0]
    assert (f.provider, f.src) == ("claude", "session-cost")
    assert round(f.session_pct, 2) == 6.0  # 15 / 250
    assert f.weekly_pct == base.UNKNOWN


def test_a_quiet_transcript_is_not_the_current_session(tmp_path):
    _project(tmp_path, [_msg("a", out=1_000_000)], mtime=NOW - 3 * 3600)
    p = _provider(tmp_path)
    p._refresh()
    assert p.poll(NOW) == []


def test_the_most_recently_written_session_wins(tmp_path):
    old = _project(tmp_path, [_msg("a", out=1_000_000)], mtime=NOW - 600)
    d2 = tmp_path / "-other"
    d2.mkdir()
    new = d2 / "s2.jsonl"
    new.write_text(_msg("b", out=2_000_000))
    os.utime(new, (NOW - 10, NOW - 10))
    assert old.exists()
    p = _provider(tmp_path)
    p._refresh()
    assert round(p.poll(NOW)[0].session_pct, 2) == 12.0  # 30 / 250


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
