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
