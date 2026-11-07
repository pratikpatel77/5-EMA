"""The rules, checked against the PDF and the Amibroker formula.

    python -m pytest tests -q
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ema5 import strategy as S  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def daily(rows: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    """rows of (date, open, high, low, close)."""
    frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    frame["volume"] = 1000.0
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date").sort_index().astype(float)


def month_of(year: int, month: int, open_: float, high: float, low: float, close: float):
    """Three trading days that add up to one monthly candle."""
    return [
        (f"{year:04d}-{month:02d}-05", open_, (open_ + high) / 2, low, (open_ + close) / 2),
        (f"{year:04d}-{month:02d}-15", (open_ + close) / 2, high, low, close),
        (f"{year:04d}-{month:02d}-25", close, high, (low + close) / 2, close),
    ]


def flat_months(count: int, start_close: float = 100.0, step: float = -1.0, year=2024, month=1):
    """`count` months walking by `step` each month."""
    rows, close = [], start_close
    for i in range(count):
        y, m = divmod((year * 12 + month - 1) + i, 12)
        rows += month_of(y, m + 1, close, close + 2, close - 2, close + step)
        close += step
    return rows


# --------------------------------------------------------------------------- #
# monthly bars
# --------------------------------------------------------------------------- #
def test_to_monthly_aggregates_each_calendar_month():
    frame = daily(month_of(2026, 3, 100, 118, 95, 110) + month_of(2026, 4, 110, 125, 104, 120))
    monthly = S.to_monthly(frame)
    assert len(monthly) == 2
    march = monthly.iloc[0]
    assert march["open"] == 100
    assert march["high"] == 118
    assert march["low"] == 95
    assert march["close"] == 110
    # stamped with the last trading day of the month
    assert monthly.index[0].day == 25


def test_to_monthly_handles_an_empty_frame():
    assert S.to_monthly(pd.DataFrame()).empty


def test_monthly_ema_is_the_recursive_ema():
    frame = daily(flat_months(8, 100.0, 5.0))
    monthly = S.to_monthly(frame)
    values = monthly["close"].tolist()
    expected, alpha = values[0], 2 / 6
    for close in values[1:]:
        expected = alpha * close + (1 - alpha) * expected
    assert S.monthly_ema(monthly).iloc[-1] == pytest.approx(expected)


def test_completed_drops_the_month_in_progress():
    frame = daily(month_of(2026, 10, 100, 110, 95, 105) + month_of(2026, 11, 105, 112, 100, 108))
    monthly = S.to_monthly(frame)
    done = S.completed(monthly, asof=date(2026, 11, 7))
    assert len(done) == 1
    assert done.index[-1].month == 10


# --------------------------------------------------------------------------- #
# the alert candle
# --------------------------------------------------------------------------- #
def test_falling_market_marks_the_latest_month_as_the_alert_candle():
    # A steady decline keeps every high below the 5 EMA.
    frame = daily(flat_months(24, 200.0, -4.0))
    result = S.evaluate(frame, asof=date(2026, 2, 1))
    assert result["status"] in (S.STATUS_PENDING, S.STATUS_TRIGGERED)
    assert result["alert_high"] < result["ema5"]


def test_a_month_whose_high_clears_the_ema_is_ignored_not_fatal():
    rows = flat_months(20, 200.0, -4.0)          # long decline -> alert candles
    close = 200.0 - 4.0 * 20
    # one strong month that pokes above the EMA, then a quiet one below it
    y, m = divmod(2024 * 12 + 20, 12)
    rows += month_of(y, m + 1, close, close + 60, close - 1, close + 55)
    y, m = divmod(2024 * 12 + 21, 12)
    rows += month_of(y, m + 1, close + 55, close + 56, close + 40, close + 45)
    frame = daily(rows)
    monthly = S.to_monthly(frame)
    done = S.completed(monthly, asof=date(2026, 12, 1))
    ema = S.monthly_ema(done)
    below = done["high"].to_numpy() < ema.to_numpy()
    assert not below[-2], "the spike month should sit above the EMA"
    # the alert candle is the most recent month that IS below
    result = S.evaluate(frame, asof=date(2026, 12, 1))
    expected = done.index[max(i for i, flag in enumerate(below) if flag)]
    assert result["alert_month"] == f"{expected.year:04d}-{expected.month:02d}"


def test_short_history_is_refused():
    frame = daily(flat_months(6, 100.0, -1.0))
    assert S.evaluate(frame)["status"] == S.STATUS_SHORT_HISTORY


def test_no_data_is_refused():
    assert S.evaluate(pd.DataFrame())["status"] == S.STATUS_NO_DATA


def test_trigger_applies_the_buffer_above_the_alert_high():
    assert S.trigger_price(1000.0, 0.1) == pytest.approx(1001.0)
    assert S.trigger_price(1000.0, 0.0) == pytest.approx(1000.0)


def test_entry_already_crossed_reports_triggered():
    rows = flat_months(20, 200.0, -4.0)
    close = 200.0 - 4.0 * 20
    # a later month that blows through the previous month's high
    y, m = divmod(2024 * 12 + 20, 12)
    rows += month_of(y, m + 1, close, close + 80, close - 2, close + 75)
    frame = daily(rows)
    result = S.evaluate(frame, asof=date(2026, 12, 1))
    assert result["status"] == S.STATUS_TRIGGERED
    assert result["triggered_on"]


def test_distance_is_the_rise_still_needed():
    assert S.distance_pct(102.0, 100.0) == pytest.approx(2.0)
    assert S.distance_pct(100.0, 100.0) == pytest.approx(0.0)
    assert S.distance_pct(98.0, 100.0) == pytest.approx(-2.0)
    assert S.distance_pct(100.0, 0.0) is None


# --------------------------------------------------------------------------- #
# the trailing stop -- the published table
# --------------------------------------------------------------------------- #
ENTRY, STOP = 3584.0, 3056.0      # the worked example in the PDF; R = 528


@pytest.mark.parametrize("peak_r, expected", [
    (0.0, 3056.0),      # untouched
    (1.9, 3056.0),      # below 2R the hard stop stands
    (2.0, 3584.0),      # breakeven
    (2.9, 3584.0),
    (3.0, 4112.0),      # +1R
    (4.0, 4640.0),      # +2R
    (5.0, 5168.0),      # +3R
    (6.0, 5696.0),      # +4R
])
def test_trail_stop_matches_the_published_table(peak_r, expected):
    assert S.trail_stop(ENTRY, STOP, peak_r) == pytest.approx(expected)


def test_beyond_four_r_the_stop_follows_two_r_behind():
    assert S.trail_stop(ENTRY, STOP, 7.5) == pytest.approx(ENTRY + 5.5 * 528)


def test_the_stop_never_moves_down():
    levels = [S.trail_stop(ENTRY, STOP, r / 10) for r in range(0, 120)]
    assert levels == sorted(levels)


def test_trail_stop_refuses_a_stop_above_the_entry():
    assert S.trail_stop(100.0, 100.0, 5.0) == 100.0


def test_ladder_rows_line_up_with_the_pdf():
    rows = {row["r"]: row for row in S.ladder(ENTRY, STOP, 4)}
    assert rows[2]["target"] == pytest.approx(4640.0)
    assert rows[2]["stop"] == pytest.approx(3584.0)
    assert rows[3]["target"] == pytest.approx(5168.0)
    assert rows[3]["stop"] == pytest.approx(4112.0)
    assert rows[5]["target"] == pytest.approx(6224.0)
    assert rows[5]["stop"] == pytest.approx(5168.0)


# --------------------------------------------------------------------------- #
# replaying a trade
# --------------------------------------------------------------------------- #
def test_track_exits_when_the_low_breaks_the_initial_stop():
    frame = daily([
        ("2026-01-01", 100, 104, 99, 103),
        ("2026-01-02", 103, 105, 101, 102),
        ("2026-01-03", 102, 103, 89, 90),      # breaks a stop at 90
    ])
    run = S.track(frame, entry=100.0, stop=90.0, entry_date=date(2026, 1, 1))
    assert run["exit"]["date"] == "2026-01-03"
    assert run["exit"]["price"] == pytest.approx(90.0)
    assert run["exit"]["r"] == pytest.approx(-1.0)


def test_a_gap_below_the_stop_fills_at_the_open():
    frame = daily([
        ("2026-01-01", 100, 104, 99, 103),
        ("2026-01-02", 84, 86, 80, 85),        # opened below the 90 stop
    ])
    run = S.track(frame, entry=100.0, stop=90.0, entry_date=date(2026, 1, 1))
    assert run["exit"]["price"] == pytest.approx(84.0)
    assert run["exit"]["gap"] is True
    assert run["exit"]["r"] < -1


def test_the_same_bar_cannot_both_lift_the_stop_and_be_saved_by_it():
    # Day 2 runs to 2.5R, which lifts the stop to breakeven, and then falls
    # back to 96. The stop in force that morning was still the original 90, so
    # the day does not exit -- a bar must never be stopped out by a level its
    # own high created.
    frame = daily([
        ("2026-01-01", 100, 101, 99, 100),
        ("2026-01-02", 100, 125, 96, 97),      # R = 10, so the high is +2.5R
        ("2026-01-03", 101, 102, 94, 95),      # opens above breakeven, then breaks it
    ])
    run = S.track(frame, entry=100.0, stop=90.0, entry_date=date(2026, 1, 1))
    # Day 2's low of 96 did not exit: the stop in force that morning was still 90.
    assert run["exit"]["date"] == "2026-01-03"
    assert run["exit"]["price"] == pytest.approx(100.0)   # breakeven, not 90
    assert run["exit"]["gap"] is False


def test_track_reports_the_live_stop_and_locked_profit():
    frame = daily([
        ("2026-01-01", 100, 110, 99, 109),
        ("2026-01-02", 109, 135, 108, 134),    # peak +3.5R on R = 10
    ])
    run = S.track(frame, entry=100.0, stop=90.0, entry_date=date(2026, 1, 1))
    assert run["exit"] is None
    assert run["peak_r"] == pytest.approx(3.5)
    assert run["stop_now"] == pytest.approx(110.0)        # entry + 1R
    assert run["locked_r"] == pytest.approx(1.0)
    assert run["peak_price"] == pytest.approx(135.0)


def test_track_before_any_bars_is_harmless():
    frame = daily([("2026-01-01", 100, 101, 99, 100)])
    run = S.track(frame, entry=100.0, stop=90.0, entry_date=date(2026, 6, 1))
    assert run["exit"] is None
    assert run["stop_now"] == 90.0


def test_monthly_series_marks_the_month_in_progress():
    frame = daily(month_of(2026, 10, 100, 110, 95, 105) + month_of(2026, 11, 105, 112, 100, 108))
    rows = S.monthly_series(frame, months=5, asof=date(2026, 11, 7))
    assert rows[-1]["forming"] is True
    assert rows[-2]["forming"] is False
    assert rows[-1]["month"] == "2026-11"


def test_daily_series_returns_recent_candles_with_iso_dates():
    frame = pd.DataFrame(
        {"open": [1.0, 2.0, 3.0], "high": [2.0, 3.0, 4.0], "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5]},
        index=pd.to_datetime(["2026-11-03", "2026-11-04", "2026-11-05"]))
    rows = S.daily_series(frame, bars=2)
    assert [r["date"] for r in rows] == ["2026-11-04", "2026-11-05"]
    assert rows[-1]["high"] == 4.0
