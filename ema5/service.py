"""Scanning, tracking and the payload the dashboard renders.

Two loops, because they cost very different amounts:

  full scan      daily bars for the whole universe, monthly resample, alert
                 candle, entry band. Minutes on a cold cache, seconds on a
                 warm one. Manual, or every N minutes if you set that.
  price refresh  live LTP for the few rows on screen (candidates plus open
                 trades) and the numbers derived from them. Seconds, so it
                 can run while you watch.

Debt is looked up only for stocks that reach the band, once a week each.
"""
from __future__ import annotations

import csv
import math
import pickle
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import bridge, strategy
from .fundamentals import VERDICTS, DebtBook
from .quotes import LivePrices, ist_now, market_open
from .store import STATUS_IGNORED, STATUS_TAKEN, Store, entry_id, now_iso

APP_ROOT = Path(__file__).resolve().parent.parent
CACHE = APP_ROOT / ".cache"
EXCLUDE = {"__NIFTY50__"}
SNAPSHOT_VERSION = 1


def _clean(value):
    """Make a payload JSON-safe.

    Two things bite here: JSON refuses NaN and infinity, so a missing number
    must travel as null; and numpy scalars come back from pandas all over the
    place. `numpy.float64` passes as a float, but `numpy.bool_` is not a
    `bool` and stops the whole response dead, so every numpy scalar is
    unwrapped rather than only the ones that have caused trouble so far.
    """
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, int):
        return int(value)
    unwrap = getattr(value, "item", None)  # numpy scalar
    if callable(unwrap):
        try:
            return _clean(unwrap())
        except (TypeError, ValueError):
            return None
    return value


class ScanError(Exception):
    pass


class Desk:
    def __init__(self):
        CACHE.mkdir(parents=True, exist_ok=True)
        self.store = Store(CACHE / "state.json")
        self.prices = LivePrices()
        self.debt = DebtBook(CACHE / "debt.json", self.store.settings()["debt_ttl_days"])
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._bars: dict[str, object] = {}
        self._setups: dict[str, dict] = {}
        self._quotes: dict[str, dict] = {}
        self._quote_meta: dict = {}
        self._universe_label = ""
        self._saved_at: str | None = None
        self.scan_state = {"status": "idle", "message": "No scan yet.", "from_saved": False, "done": 0,
                           "total": 0, "started_at": None, "finished_at": None, "errors": []}
        self._auto_stop = threading.Event()
        self._auto_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # universe
    # ------------------------------------------------------------------ #
    def _universe(self, instruments: dict[str, str], top: int) -> tuple[dict[str, str], str]:
        path = bridge.universe_file()
        if not path:
            picked = {k: v for k, v in list(instruments.items())[:top] if k not in EXCLUDE}
            return picked, f"broker symbol list, first {len(picked)}"
        wanted: list[str] = []
        with path.open(newline="", encoding="utf-8-sig", errors="ignore") as handle:
            for row in csv.DictReader(handle):
                symbol = (row.get("Symbol") or "").strip().upper()
                if symbol and symbol not in EXCLUDE:
                    wanted.append(symbol)
                if len(wanted) >= top:
                    break
        picked = {}
        for symbol in wanted:
            for variant in (symbol, symbol.replace("_", "-")):
                if variant in instruments:
                    picked[variant] = instruments[variant]
                    break
        return picked, f"top {len(picked)} NSE stocks by market cap"

    # ------------------------------------------------------------------ #
    # the full scan
    # ------------------------------------------------------------------ #
    def scan(self, offline: bool = False, refresh: bool = False) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            raise ScanError("A scan is already running.")
        try:
            return self._scan(offline=offline, refresh=refresh)
        finally:
            self._scan_lock.release()

    def _progress(self, **changes):
        with self._lock:
            self.scan_state.update(changes)

    def _scan(self, offline: bool, refresh: bool) -> dict:
        settings = self.store.settings()
        started = time.time()
        self._progress(status="running", message="Signing in to the data source...",
                       done=0, total=0, started_at=now_iso(), errors=[])
        bridge.ensure()
        from datalayer import BarStore, get_source

        errors: list[str] = []
        try:
            source = get_source(settings["source"])
            if not offline:
                source.authenticate()
            instruments = source.instruments()
        except Exception as exc:
            self._scan_failed(exc)
            raise ScanError(str(exc)) from exc

        chosen, label = self._universe(instruments, int(settings["top_n"]))
        self._progress(message=f"Loading daily bars for {len(chosen)} stocks...",
                       total=len(chosen))

        store = BarStore(source, history_days=int(settings["history_days"]),
                         refresh=refresh, offline=offline)
        watcher = threading.Thread(target=self._watch_bars, args=(store, len(chosen)), daemon=True)
        watcher.start()
        try:
            bars = store.load_many(chosen, progress=False)
        except Exception as exc:
            self._scan_failed(exc)
            raise ScanError(str(exc)) from exc
        finally:
            self._bars_done = True

        self._progress(message=f"Reading monthly candles for {len(bars)} stocks...",
                       done=len(bars), total=len(bars))
        buffer_pct = float(settings["buffer_pct"])
        today = date.today()
        setups = {}
        for symbol, frame in bars.items():
            try:
                setups[symbol] = strategy.evaluate(frame, buffer_pct=buffer_pct, asof=today)
            except Exception:
                continue

        with self._lock:
            self._bars = bars
            self._setups = setups
            self._universe_label = label

        self._progress(message="Fetching live prices...")
        self._refresh_quotes(self._watch_symbols(setups, settings))

        self._progress(message="Checking debt levels...")
        try:
            self._fill_debt(settings)
        except Exception as exc:
            errors.append(f"Debt lookup: {exc}")

        if store.stats.get("failed"):
            errors.append(f"{store.stats['failed']} stocks had no usable price history.")
        finished = now_iso()
        try:
            self._save_snapshot(finished)
        except Exception as exc:  # losing the save must not lose the scan
            errors.append(f"Could not save this scan: {exc}")
        self._progress(status="done", finished_at=finished, from_saved=False, errors=errors[:4],
                       message=f"Scanned {len(bars)} stocks in {time.time() - started:.0f}s "
                               f"({store.stats.get('cached', 0)} cached, "
                               f"{store.stats.get('updated', 0) + store.stats.get('fetched', 0)} downloaded).")
        return self.state()

    # ------------------------------------------------------------------ #
    # saved scan
    # ------------------------------------------------------------------ #
    def _scan_failed(self, exc: Exception):
        """A failed scan must not erase, or re-date, the results already on screen."""
        with self._lock:
            kept = self._saved_at if self._setups else None
        self._progress(status="failed", message=str(exc)[:300], finished_at=kept)

    def _save_snapshot(self, saved_at: str):
        """Keep the setups for the whole universe, plus daily bars for the stocks
        that can matter before the next scan: anything within reach of its
        trigger and every tracked trade. That is what lets the desk open
        straight onto live candidates and open trades."""
        settings = self.store.settings()
        reach = max(float(settings["approach_pct"]) * 2, 12.0)
        with self._lock:
            setups, bars, label = dict(self._setups), dict(self._bars), self._universe_label
        keep = {s for s, r in setups.items()
                if r.get("status") == strategy.STATUS_PENDING
                and r.get("distance_pct") is not None and -1.0 <= r["distance_pct"] <= reach}
        keep |= {e["symbol"] for e in self.store.entries().values()}
        payload = {
            "version": SNAPSHOT_VERSION, "saved_at": saved_at, "label": label,
            "buffer_pct": float(settings["buffer_pct"]), "top_n": int(settings["top_n"]),
            "setups": setups, "bars": {s: bars[s] for s in keep if s in bars},
        }
        path = CACHE / "scan.pkl"
        tmp = path.with_suffix(".tmp")
        with tmp.open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
        self._saved_at = saved_at

    def load_snapshot(self) -> bool:
        """Open on the last scan. Returns False when there is none (or it is unreadable)."""
        path = CACHE / "scan.pkl"
        if not path.exists():
            return False
        try:
            with path.open("rb") as handle:
                payload = pickle.load(handle)
            if payload.get("version") != SNAPSHOT_VERSION:
                return False
            setups, bars = payload["setups"], payload["bars"]
            saved_at = payload["saved_at"]
        except Exception:
            return False
        with self._lock:
            self._setups, self._bars = setups, bars
            self._universe_label = payload.get("label", "")
            self._saved_at = saved_at
        stale_month = saved_at[:7] != date.today().isoformat()[:7]
        note = ("A new month has started since, so alert candles will change once the fresh scan finishes. "
                if stale_month else "")
        self._progress(status="done", finished_at=saved_at, from_saved=True, errors=[],
                       message=f"Showing the saved scan from {saved_at[:16].replace('T', ' ')}. {note}"
                               "Live prices are refreshed; a full scan follows.")
        return True

    def _watch_bars(self, store, total: int):
        """Publish download progress while load_many runs."""
        self._bars_done = False
        while not getattr(self, "_bars_done", True):
            stats = store.stats
            done = stats.get("cached", 0) + stats.get("updated", 0) + stats.get("fetched", 0) + stats.get("failed", 0)
            self._progress(done=done, total=total)
            time.sleep(0.4)

    # ------------------------------------------------------------------ #
    # bands
    # ------------------------------------------------------------------ #
    def _watch_symbols(self, setups: dict, settings: dict) -> list[str]:
        """Symbols worth a live price: anything in the wider band, plus open trades."""
        reach = float(settings["approach_pct"])
        out = [s for s, r in setups.items()
               if r.get("status") == strategy.STATUS_PENDING
               and r.get("distance_pct") is not None and -1.0 <= r["distance_pct"] <= reach]
        out += [e["symbol"] for e in self.store.taken()]
        return list(dict.fromkeys(out))[:300]

    def _refresh_quotes(self, symbols: list[str]):
        if not symbols:
            return
        try:
            result = self.prices.quotes(symbols)
        except Exception as exc:
            with self._lock:
                self._quote_meta = {"error": str(exc)[:200], "fetched_at": now_iso()}
            return
        with self._lock:
            self._quotes.update(result["quotes"])
            self._quote_meta = {k: v for k, v in result.items() if k != "quotes"}

    def _price(self, symbol: str, setup: dict) -> tuple[float, dict]:
        """Live price when there is one, otherwise the last daily close."""
        quote = self._quotes.get(symbol)
        if quote and quote.get("ltp"):
            return float(quote["ltp"]), quote
        return float(setup.get("last_close") or 0.0), {"source": "Daily close",
                                                       "as_of": setup.get("last_bar")}

    def _fill_debt(self, settings: dict):
        """One lookup per stock that has reached the narrow band."""
        band = float(settings["max_distance_pct"])
        hot = []
        for symbol, setup in self._setups.items():
            if setup.get("status") != strategy.STATUS_PENDING:
                continue
            price, _ = self._price(symbol, setup)
            distance = strategy.distance_pct(setup["trigger"], price)
            if distance is not None and -0.5 <= distance <= band:
                hot.append(symbol)
        for symbol in hot[:40]:
            try:
                self.debt.get(symbol)
            except Exception:
                continue

    # ------------------------------------------------------------------ #
    # rows
    # ------------------------------------------------------------------ #
    def _candidate(self, symbol: str, setup: dict, settings: dict) -> dict:
        price, quote = self._price(symbol, setup)
        distance = strategy.distance_pct(setup["trigger"], price)
        record = self.debt.cached(symbol)
        verdict = (record or {}).get("verdict", "unknown")
        key = entry_id(symbol, setup["alert_month"])
        return {
            "id": key,
            "symbol": symbol,
            "alert_month": setup["alert_month"],
            "alert_month_end": setup.get("alert_month_end"),
            "alert_high": setup["alert_high"],
            "alert_low": setup["alert_low"],
            "ema5": setup["ema5"],
            "trigger": setup["trigger"],
            "stop": setup["stop"],
            "risk": setup["risk"],
            "risk_pct": setup["risk_pct"],
            "ltp": price,
            "price_source": quote.get("source"),
            "quote_age_seconds": quote.get("quote_age_seconds"),
            "change_pct": quote.get("change_pct"),
            "distance_pct": distance,
            "distance_rupees": setup["trigger"] - price,
            "months_since_alert": setup.get("months_since_alert"),
            "last_bar": setup.get("last_bar"),
            "debt": record,
            "debt_verdict": verdict,
            "debt_label": VERDICTS.get(verdict, VERDICTS["unknown"])[0],
            "debt_pass": self.debt.passes(record, float(settings["max_de"])),
            "reward_2r": setup["trigger"] + 2 * setup["risk"],
            "reward_3r": setup["trigger"] + 3 * setup["risk"],
        }

    def _position(self, record: dict, settings: dict) -> dict:
        symbol = record["symbol"]
        frame = self._bars.get(symbol)
        setup = self._setups.get(symbol, {})
        entry = float(record["entry"])
        stop = float(record["stop"])
        try:
            entry_day = date.fromisoformat(record["entry_date"])
        except (KeyError, ValueError):
            entry_day = date.today()

        run = strategy.track(frame, entry, stop, entry_day) if frame is not None else {
            "peak_r": 0.0, "stop_now": stop, "locked_r": -1.0, "exit": None,
            "risk": entry - stop, "bars": 0, "peak_price": None, "peak_on": None}
        risk = entry - stop
        price, quote = self._price(symbol, setup or {"last_close": entry})
        live_r = (price - entry) / risk if risk > 0 else None

        exit_info = run.get("exit")
        manual = record.get("closed_manual")
        if manual:
            exit_info = {"date": manual.get("date"), "price": manual.get("price"),
                         "r": ((float(manual.get("price", entry)) - entry) / risk) if risk > 0 else None,
                         "manual": True, "note": manual.get("note")}

        peak_r = max(run.get("peak_r") or 0.0, live_r or 0.0) if not exit_info else run.get("peak_r") or 0.0
        stop_now = strategy.trail_stop(entry, stop, peak_r)
        steps = strategy.ladder(entry, stop, 6)
        upcoming = next((s for s in steps if s["r"] > peak_r), None)

        # An entry taken before price actually crossed the trigger is flagged,
        # so a tracked trade is never quietly fictional.
        reached = None
        if frame is not None and len(frame):
            after = frame[frame.index >= pd.Timestamp(entry_day)]
            reached = bool(len(after) and float(after["high"].max()) >= entry)

        return {
            "id": record["id"],
            "symbol": symbol,
            "alert_month": record.get("alert_month"),
            "status": "stopped" if exit_info else "open",
            "entry": entry,
            "entry_date": record.get("entry_date"),
            "stop_initial": stop,
            "risk": risk,
            "risk_pct": risk / entry * 100 if entry else None,
            "ltp": price,
            "price_source": quote.get("source"),
            "change_pct": quote.get("change_pct"),
            "r_now": live_r,
            "peak_r": peak_r,
            "peak_price": run.get("peak_price"),
            "peak_on": run.get("peak_on"),
            "stop_now": stop_now,
            "locked_r": (stop_now - entry) / risk if risk > 0 else None,
            "stop_moved": stop_now > stop + 1e-9,
            "next_step": upcoming,
            "ladder": steps,
            "exit": exit_info,
            "bars_held": run.get("bars"),
            "note": record.get("note") or "",
            "trigger_at_take": record.get("trigger_at_take"),
            "entry_reached": reached,
            "open_risk": (price - stop_now) if not exit_info else None,
        }

    # ------------------------------------------------------------------ #
    # payload
    # ------------------------------------------------------------------ #
    def state(self) -> dict:
        settings = self.store.settings()
        with self._lock:
            setups = dict(self._setups)
        entries = self.store.entries()
        taken_ids = {k for k, e in entries.items() if e.get("status") == STATUS_TAKEN}
        ignored_ids = {k for k, e in entries.items() if e.get("status") == STATUS_IGNORED}

        band = float(settings["max_distance_pct"])
        reach = float(settings["approach_pct"])
        max_de = float(settings["max_de"])

        candidates, approaching, hidden_debt = [], [], []
        for symbol, setup in setups.items():
            if setup.get("status") != strategy.STATUS_PENDING:
                continue
            row = self._candidate(symbol, setup, settings)
            if row["id"] in taken_ids or row["id"] in ignored_ids:
                continue
            distance = row["distance_pct"]
            if distance is None:
                continue
            if 0 <= distance <= band:
                if settings["low_debt_only"] and row["debt_pass"] is False:
                    hidden_debt.append(row)
                else:
                    candidates.append(row)
            elif band < distance <= reach:
                approaching.append(row)

        candidates.sort(key=lambda r: r["distance_pct"])
        approaching.sort(key=lambda r: r["distance_pct"])
        hidden_debt.sort(key=lambda r: r["distance_pct"])

        positions, closed = [], []
        for record in entries.values():
            if record.get("status") != STATUS_TAKEN:
                continue
            row = self._position(record, settings)
            (closed if row["status"] == "stopped" else positions).append(row)
        positions.sort(key=lambda r: (r["r_now"] is None, -(r["r_now"] or 0)))
        closed.sort(key=lambda r: (r["exit"] or {}).get("date") or "", reverse=True)

        ignored = []
        for key in sorted(ignored_ids):
            record = entries[key]
            setup = setups.get(record["symbol"])
            row = {"id": key, "symbol": record["symbol"], "alert_month": record.get("alert_month"),
                   "note": record.get("note") or "", "updated_at": record.get("updated_at")}
            if setup and setup.get("status") == strategy.STATUS_PENDING:
                live = self._candidate(record["symbol"], setup, settings)
                row.update(trigger=live["trigger"], ltp=live["ltp"],
                           distance_pct=live["distance_pct"], still_valid=True)
            ignored.append(row)

        realised = [r["exit"]["r"] for r in closed if (r.get("exit") or {}).get("r") is not None]
        statuses: dict[str, int] = {}
        for setup in setups.values():
            statuses[setup.get("status", "unknown")] = statuses.get(setup.get("status", "unknown"), 0) + 1

        with self._lock:
            scan = dict(self.scan_state)
            meta = dict(self._quote_meta)
        return _clean({
            "scan": scan,
            "settings": settings,
            "universe": {"label": self._universe_label, "scanned": len(setups),
                         "statuses": statuses},
            "market": {"open": market_open(), "now": ist_now().isoformat(timespec="seconds"),
                       **meta},
            "candidates": candidates,
            "approaching": approaching,
            "hidden_by_debt": hidden_debt,
            "positions": positions,
            "closed": closed,
            "ignored": ignored,
            "totals": {
                "candidates": len(candidates),
                "approaching": len(approaching),
                "hidden_by_debt": len(hidden_debt),
                "open": len(positions),
                "closed": len(closed),
                "open_r": sum(r["r_now"] for r in positions if r.get("r_now") is not None),
                "locked_r": sum(r["locked_r"] for r in positions
                                if r.get("locked_r") is not None and r["locked_r"] > 0),
                "realised_r": sum(realised),
                "wins": sum(1 for r in realised if r > 0),
                "losses": sum(1 for r in realised if r <= 0),
                "max_de": max_de,
            },
            "debt_legend": {k: {"label": v[0], "note": v[1]} for k, v in VERDICTS.items()},
        })

    def refresh_prices(self) -> dict:
        """Live prices for the rows on screen. Cheap enough to call every few seconds."""
        settings = self.store.settings()
        with self._lock:
            setups = dict(self._setups)
        self._refresh_quotes(self._watch_symbols(setups, settings))
        return self.state()

    # ------------------------------------------------------------------ #
    # detail
    # ------------------------------------------------------------------ #
    def detail(self, symbol: str, refresh_debt: bool = False) -> dict:
        symbol = (symbol or "").upper()
        frame = self._bars.get(symbol)
        if frame is None:
            raise KeyError(f"{symbol} was not in the last scan.")
        settings = self.store.settings()
        setup = self._setups.get(symbol) or {}
        out = {
            "symbol": symbol,
            "setup": setup,
            "months": strategy.monthly_series(frame, 30),
            "debt": self.debt.get(symbol, refresh=refresh_debt) if refresh_debt
                    else (self.debt.cached(symbol) or self.debt.get(symbol)),
        }
        if setup.get("status") == strategy.STATUS_PENDING:
            out["candidate"] = self._candidate(symbol, setup, settings)
            out["ladder"] = strategy.ladder(setup["trigger"], setup["stop"], 6)
        key = entry_id(symbol, setup.get("alert_month", ""))
        record = self.store.get(key)
        if record and record.get("status") == STATUS_TAKEN:
            out["position"] = self._position(record, settings)
        return _clean(out)

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #
    def take(self, request: dict) -> dict:
        symbol = str(request.get("symbol", "")).upper().strip()
        setup = self._setups.get(symbol)
        if not setup or setup.get("status") not in (strategy.STATUS_PENDING, strategy.STATUS_TRIGGERED):
            raise ValueError(f"{symbol} has no live 5 EMA setup in the last scan.")
        month = setup["alert_month"]
        entry = request.get("entry")
        entry = float(entry) if entry not in (None, "") else float(setup["trigger"])
        stop = request.get("stop")
        stop = float(stop) if stop not in (None, "") else float(setup["stop"])
        if not (entry > 0 and stop > 0):
            raise ValueError("Entry and stop must both be positive.")
        if stop >= entry:
            raise ValueError("The stop must sit below the entry price.")
        entry_date = str(request.get("entry_date") or date.today().isoformat())
        try:
            parsed = date.fromisoformat(entry_date)
        except ValueError:
            raise ValueError("Use an entry date like 2026-11-07.") from None
        if parsed > date.today() + timedelta(days=1):
            raise ValueError("The entry date cannot be in the future.")

        record = self.store.put({
            "id": entry_id(symbol, month),
            "symbol": symbol,
            "alert_month": month,
            "status": STATUS_TAKEN,
            "entry": entry,
            "stop": stop,
            "entry_date": parsed.isoformat(),
            "trigger_at_take": setup["trigger"],
            "alert_high": setup["alert_high"],
            "alert_low": setup["alert_low"],
            "note": str(request.get("note") or "")[:400],
        })
        return _clean(self._position(record, self.store.settings()))

    def ignore(self, request: dict) -> dict:
        symbol = str(request.get("symbol", "")).upper().strip()
        setup = self._setups.get(symbol)
        if not setup or not setup.get("alert_month"):
            raise ValueError(f"{symbol} has no live 5 EMA setup in the last scan.")
        record = self.store.put({
            "id": entry_id(symbol, setup["alert_month"]),
            "symbol": symbol,
            "alert_month": setup["alert_month"],
            "status": STATUS_IGNORED,
            "note": str(request.get("note") or "")[:400],
        })
        return record

    def update_entry(self, request: dict) -> dict:
        key = str(request.get("id") or "")
        record = self.store.get(key)
        if not record:
            raise KeyError("That entry is not being tracked.")
        changes: dict = {}
        if request.get("entry") not in (None, ""):
            changes["entry"] = float(request["entry"])
        if request.get("stop") not in (None, ""):
            changes["stop"] = float(request["stop"])
        if request.get("entry_date"):
            changes["entry_date"] = date.fromisoformat(str(request["entry_date"])).isoformat()
        if "note" in request:
            changes["note"] = str(request.get("note") or "")[:400]
        entry = changes.get("entry", record.get("entry"))
        stop = changes.get("stop", record.get("stop"))
        if float(stop) >= float(entry):
            raise ValueError("The stop must sit below the entry price.")
        return _clean(self._position(self.store.patch(key, changes), self.store.settings()))

    def close_entry(self, request: dict) -> dict:
        key = str(request.get("id") or "")
        record = self.store.get(key)
        if not record:
            raise KeyError("That entry is not being tracked.")
        price = request.get("price")
        if price in (None, ""):
            raise ValueError("Give the exit price you are recording.")
        closed = {"date": str(request.get("date") or date.today().isoformat()),
                  "price": float(price), "note": str(request.get("note") or "")[:400]}
        date.fromisoformat(closed["date"])
        return _clean(self._position(self.store.patch(key, {"closed_manual": closed}),
                                     self.store.settings()))

    def forget(self, key: str) -> bool:
        return self.store.delete(key)

    def restore(self, key: str) -> bool:
        """Put an ignored stock back on the watchlist."""
        record = self.store.get(key)
        if not record or record.get("status") != STATUS_IGNORED:
            return False
        return self.store.delete(key)

    def lookup_debt(self, symbol: str) -> dict:
        return self.debt.get((symbol or "").upper(), refresh=True)

    def save_settings(self, changes: dict) -> dict:
        settings = self.store.save_settings(changes)
        self.debt.ttl = float(settings["debt_ttl_days"]) * 86400
        self._schedule_auto(settings)
        return settings

    # ------------------------------------------------------------------ #
    # optional auto scan
    # ------------------------------------------------------------------ #
    def start(self):
        self._schedule_auto(self.store.settings())

    def _schedule_auto(self, settings: dict):
        self._auto_stop.set()
        if self._auto_thread and self._auto_thread.is_alive():
            self._auto_thread.join(timeout=1.0)
        minutes = int(settings.get("auto_scan_minutes") or 0)
        if minutes <= 0:
            return
        self._auto_stop = threading.Event()
        stop = self._auto_stop

        def loop():
            while not stop.wait(minutes * 60):
                if not market_open():
                    continue
                try:
                    self.scan()
                except Exception:
                    pass

        self._auto_thread = threading.Thread(target=loop, daemon=True)
        self._auto_thread.start()

    def close(self):
        self._auto_stop.set()
