"""Definedge's REST quote has a clock time but no date, and keeps serving a
frozen mid-session snapshot after the close. These pin down when it is trusted."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ema5 import quotes as Q  # noqa: E402

IST = Q.IST


def at(hh, mm, ss=0):
    return datetime(2026, 11, 6, hh, mm, ss, tzinfo=IST)   # a Friday


def test_age_is_measured_from_the_clock_time():
    assert Q.trade_age_seconds("11:30:35", now=at(11, 31, 5)) == pytest.approx(30.0)


@pytest.mark.parametrize("value", [None, "", "00:00:00", "garbage", "25:61:00"])
def test_missing_or_malformed_times_have_no_age(value):
    assert Q.trade_age_seconds(value, now=at(12, 0)) is None


def test_a_time_later_today_is_rejected_not_treated_as_fresh():
    # 15:00 "now" with a 15:20 trade time can only be yesterday's snapshot
    assert Q.trade_age_seconds("15:20:00", now=at(15, 0)) is None


def test_market_hours():
    assert Q.market_open(at(10, 0)) is True
    assert Q.market_open(at(9, 0)) is False
    assert Q.market_open(at(15, 31)) is False
    assert Q.market_open(datetime(2026, 11, 7, 11, 0, tzinfo=IST)) is False   # Saturday


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def definedge_prices(monkeypatch, payload, now):
    prices = Q.LivePrices()

    class Source:
        session_key = "k"

        class _limiter:
            @staticmethod
            def wait():
                pass

    monkeypatch.setattr(prices, "_definedge_source", lambda: (Source, {"AAA": "1"}))
    monkeypatch.setattr(prices.session, "get", lambda *a, **k: FakeResponse(payload))
    monkeypatch.setattr(Q, "ist_now", lambda: now)
    return prices._definedge_one("AAA")[1]


def payload(clock):
    return {"status": "SUCCESS", "ltp": "6622.00", "last_trade_time": clock,
            "day_high": "6719.5", "day_low": "6294.5", "day_close": None}


def test_a_frozen_snapshot_after_the_close_is_not_used(monkeypatch):
    # exactly the ASTRAZEN case: 11:30:35 shown on a Saturday
    saturday = datetime(2026, 11, 7, 18, 0, tzinfo=IST)
    assert definedge_prices(monkeypatch, payload("11:30:35"), saturday) is None


def test_a_stale_quote_during_the_session_is_not_used(monkeypatch):
    assert definedge_prices(monkeypatch, payload("11:30:35"), at(14, 0)) is None


def test_a_fresh_quote_during_the_session_is_used(monkeypatch):
    quote = definedge_prices(monkeypatch, payload("11:59:40"), at(12, 0, 0))
    assert quote["ltp"] == 6622.0
    assert quote["quote_age_seconds"] == pytest.approx(20.0)
    assert quote["source"] == "Definedge"
