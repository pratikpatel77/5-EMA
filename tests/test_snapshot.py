"""The desk reopens on its last scan: candidates and tracked trades come
straight back, without a download and without a fresh scan."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_strategy import daily, flat_months, month_of  # noqa: E402

from ema5 import service as svc  # noqa: E402
from ema5 import strategy as S  # noqa: E402


@pytest.fixture()
def desk_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "CACHE", tmp_path)

    def make():
        desk = svc.Desk()
        # no network: debt is unknown, prices fall back to the daily close
        monkeypatch.setattr(desk.debt, "get", lambda *a, **k: {})
        monkeypatch.setattr(desk.prices, "quotes",
                            lambda symbols: {"quotes": {}, "missing": list(symbols), "source": None,
                                             "market_open": False})
        return desk
    return make


def near_trigger_stock():
    """A falling stock whose latest close sits ~1% under its trigger."""
    rows = flat_months(24, 200.0, -4.0, year=2024, month=1)   # through Dec 2025
    return daily(rows)


def seed(desk):
    frame = near_trigger_stock()
    setup = S.evaluate(frame, asof=date(2026, 1, 15))
    assert setup["status"] == S.STATUS_PENDING
    desk._bars = {"TESTCO": frame}
    desk._setups = {"TESTCO": setup}
    desk._universe_label = "test universe"
    return setup


def test_round_trip_restores_setups_and_bars(desk_factory):
    first = desk_factory()
    setup = seed(first)
    first._save_snapshot("2026-01-15T10:00:00+05:30")

    second = desk_factory()
    assert second.load_snapshot() is True
    assert second._setups["TESTCO"]["trigger"] == pytest.approx(setup["trigger"])
    assert len(second._bars["TESTCO"]) == len(first._bars["TESTCO"])
    state = second.scan_state
    assert state["from_saved"] is True
    assert state["status"] == "done"
    assert state["finished_at"] == "2026-01-15T10:00:00+05:30"


def test_tracked_trade_survives_a_restart(desk_factory):
    first = desk_factory()
    setup = seed(first)
    first.take({"symbol": "TESTCO", "entry": setup["trigger"], "entry_date": "2025-12-30"})
    first._save_snapshot("2026-01-15T10:00:00+05:30")

    second = desk_factory()
    assert second.load_snapshot()
    positions = second.state()["positions"] + second.state()["closed"]
    assert [p["symbol"] for p in positions] == ["TESTCO"]
    assert positions[0]["entry"] == pytest.approx(setup["trigger"])
    assert positions[0]["stop_initial"] == pytest.approx(setup["stop"])


def test_bars_are_saved_for_tracked_trades_even_when_far_from_the_trigger(desk_factory):
    first = desk_factory()
    seed(first)
    first.store.put({"id": "OTHER:2025-12", "symbol": "OTHER", "alert_month": "2025-12",
                     "status": "taken", "entry": 100.0, "stop": 90.0, "entry_date": "2026-01-02"})
    first._bars["OTHER"] = daily(month_of(2026, 1, 100, 130, 99, 125))
    first._setups["OTHER"] = {"status": S.STATUS_TRIGGERED}   # nowhere near a trigger
    first._save_snapshot("2026-01-15T10:00:00+05:30")

    second = desk_factory()
    second.load_snapshot()
    assert "OTHER" in second._bars


def test_no_snapshot_means_no_resume(desk_factory):
    assert desk_factory().load_snapshot() is False


def test_a_corrupt_snapshot_is_ignored(desk_factory, tmp_path):
    (tmp_path / "scan.pkl").write_bytes(b"not a pickle")
    assert desk_factory().load_snapshot() is False


def test_a_failed_scan_keeps_the_saved_results_and_their_date(desk_factory):
    first = desk_factory()
    seed(first)
    first._save_snapshot("2026-01-15T10:00:00+05:30")

    second = desk_factory()
    second.load_snapshot()
    second._scan_failed(RuntimeError("Definedge login failed"))
    assert second.scan_state["status"] == "failed"
    assert second.scan_state["finished_at"] == "2026-01-15T10:00:00+05:30"
    assert "TESTCO" in second._setups


# ---- debt filter -------------------------------------------------------------
# The seeded stock sits 5.9% under its trigger: with the default 2% watch band
# it is "approaching"; widening the band to 7% makes it "at the trigger".
AT_TRIGGER, APPROACHING = 7.0, 2.0


def lender_record(npa):
    return {"v": 2, "symbol": "TESTCO", "financial": True, "verdict": "lender_sound", "de": None,
            "lender": {"net_npa_pct": npa}, "source": "screener.in", "fetched_at_ts": 9e12}


def company_record(de):
    return {"v": 2, "symbol": "TESTCO", "financial": False, "verdict": "low", "de": de,
            "lender": None, "source": "screener.in", "fetched_at_ts": 9e12}


def shown(desk):
    state = desk.state()
    return ([r["symbol"] for r in state["candidates"]],
            [r["symbol"] for r in state["approaching"]],
            state["hidden_by_debt"])


def prepared(desk_factory, band, **settings):
    desk = desk_factory()
    seed(desk)
    desk.store.save_settings({"max_distance_pct": band, **settings})
    return desk


@pytest.mark.parametrize("band, view, name", [(AT_TRIGGER, 0, "candidates"), (APPROACHING, 1, "approaching")])
def test_debt_filter_shows_only_stocks_that_pass(desk_factory, band, view, name):
    desk = prepared(desk_factory, band)
    desk.debt._data["TESTCO"] = company_record(0.10)
    assert shown(desk)[view] == ["TESTCO"]

    desk.debt._data["TESTCO"] = company_record(0.90)
    candidates, approaching, hidden = shown(desk)
    assert candidates == [] and approaching == []
    assert hidden[0]["debt_why"] == "D/E 0.90" and hidden[0]["view"] == name


def test_a_lender_is_filtered_on_net_npa(desk_factory):
    desk = prepared(desk_factory, AT_TRIGGER)
    desk.debt._data["TESTCO"] = lender_record(0.4)
    assert shown(desk)[0] == ["TESTCO"]
    desk.debt._data["TESTCO"] = lender_record(2.5)
    assert shown(desk)[0] == [] and shown(desk)[2][0]["debt_why"] == "net NPA 2.50%"
    desk.store.save_settings({"max_nnpa": 3.0})
    assert shown(desk)[0] == ["TESTCO"]


def test_unknown_debt_is_skipped_not_flagged(desk_factory):
    desk = prepared(desk_factory, AT_TRIGGER)
    assert shown(desk)[0] == []                       # never looked up
    assert shown(desk)[2][0]["debt_why"] == "not checked yet"
    desk.debt._data["TESTCO"] = {**company_record(None), "verdict": "unknown", "source": None}
    assert shown(desk)[0] == [] and shown(desk)[2][0]["debt_why"] == "no debt data"


def test_filter_off_shows_everything(desk_factory):
    desk = prepared(desk_factory, AT_TRIGGER, low_debt_only=False)
    assert shown(desk)[0] == ["TESTCO"] and shown(desk)[2] == []


def test_debt_is_looked_up_for_the_approaching_band_too(desk_factory):
    desk = prepared(desk_factory, APPROACHING)         # stock is in "approaching", not at the trigger
    asked = []
    desk.debt.get = lambda symbol, *a, **k: asked.append(symbol) or {}
    desk._fill_debt(desk.store.settings())
    assert asked == ["TESTCO"]


def test_new_net_npa_setting_is_validated(desk_factory):
    desk = desk_factory()
    assert desk.store.settings()["max_nnpa"] == 1.0
    with pytest.raises(ValueError):
        desk.store.save_settings({"max_nnpa": 50})
