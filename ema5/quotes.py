"""Live last-traded prices for the handful of symbols on screen.

Fyers answers in batches of 50 and reports the trade time. When its daily
token has expired, Definedge serves instead -- it logs in by itself, so the
dashboard keeps working without anyone pasting a token. Prices are never
invented: a symbol with no live price keeps its last daily close, clearly
labelled as such.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone

import requests

from . import bridge

IST = timezone(timedelta(hours=5, minutes=30))
FYERS_QUOTES = "https://api-t1.fyers.in/data/quotes"
DEFINEDGE_QUOTE = "https://integrate.definedgesecurities.com/dart/v1/quotes/NSE/{token}"
BATCH = 50
COOLDOWN = 120.0  # seconds to leave Fyers alone after it fails


def ist_now() -> datetime:
    return datetime.now(IST)


def market_open(stamp: datetime | None = None) -> bool:
    stamp = stamp or ist_now()
    if stamp.weekday() >= 5:
        return False
    return dtime(9, 15) <= stamp.time() <= dtime(15, 30)


MAX_DEFINEDGE_AGE = 300.0  # seconds; older than this and a Definedge quote is not trusted


def trade_age_seconds(clock_text, now: datetime | None = None) -> float | None:
    """Seconds since an `HH:MM:SS` trade time, assuming it was today in IST.
    None when it is missing, malformed, or would lie in the future."""
    text = str(clock_text or "").strip()
    if not text or text == "00:00:00":
        return None
    try:
        traded = datetime.strptime(text, "%H:%M:%S").time()
    except ValueError:
        return None
    now = now or ist_now()
    age = (now - datetime.combine(now.date(), traded, IST)).total_seconds()
    return age if age >= 0 else None


def _number(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN guard


class LivePrices:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "5EMA-Desk/1.0"})
        self._lock = threading.RLock()
        self._spacing = threading.Lock()
        self._last_call = 0.0
        self._fyers_down_until = 0.0
        self._definedge = None
        self._definedge_day = None

    # -- Fyers ------------------------------------------------------------- #
    def _space(self):
        with self._spacing:
            gap = 0.12 - (time.monotonic() - self._last_call)
            if gap > 0:
                time.sleep(gap)
            self._last_call = time.monotonic()

    def _fyers_batch(self, symbols: list[str]) -> dict[str, dict]:
        bridge.ensure()  # must come before the import: startup prices run before any scan
        from datalayer import config

        app, token = config.get("FYERS_APP_ID"), config.get("FYERS_ACCESS_TOKEN")
        if not app or not token:
            raise ValueError("Fyers login missing.")
        self._space()
        response = self.session.get(
            FYERS_QUOTES,
            params={"symbols": ",".join(f"NSE:{s}-EQ" for s in symbols)},
            headers={"Authorization": f"{app}:{token}"}, timeout=(8, 25))
        if response.status_code in (401, 403):
            raise ValueError("Fyers token expired. Run tools/fyers_login.py in the scanners project.")
        if response.status_code == 429:
            raise ValueError("Fyers rate limit reached.")
        response.raise_for_status()
        payload = response.json()
        if payload.get("s") != "ok":
            raise ValueError("Fyers could not supply quotes.")
        fetched = ist_now()
        out: dict[str, dict] = {}
        for row in payload.get("d") or payload.get("data") or []:
            name = str(row.get("n", ""))
            if not (name.startswith("NSE:") and name.endswith("-EQ")) or row.get("s") != "ok":
                continue
            value = row.get("v") or {}
            price = _number(value.get("lp"))
            if price <= 0:
                continue
            stamp = _number(value.get("tt"))
            if stamp > 1e12:
                stamp /= 1000
            age = fetched.timestamp() - stamp if stamp else None
            out[name[4:-3]] = {
                "ltp": price,
                "change_pct": _number(value.get("chp")),
                "day_high": _number(value.get("high_price", value.get("h"))) or None,
                "day_low": _number(value.get("low_price", value.get("l"))) or None,
                "source": "Fyers",
                "quote_age_seconds": round(age, 1) if age is not None else None,
                "fetched_at": fetched.isoformat(timespec="seconds"),
            }
        return out

    # -- Definedge --------------------------------------------------------- #
    def _definedge_source(self):
        bridge.ensure()
        from datalayer import get_source

        today = ist_now().date()
        with self._lock:
            if self._definedge is None or self._definedge_day != today:
                source = get_source("definedge")
                source.authenticate()
                self._definedge = (source, source.instruments())
                self._definedge_day = today
            return self._definedge

    def _definedge_one(self, symbol: str) -> tuple[str, dict | None]:
        try:
            source, instruments = self._definedge_source()
        except Exception:
            return symbol, None
        token = instruments.get(symbol)
        if not token:
            return symbol, None
        source._limiter.wait()
        try:
            response = self.session.get(DEFINEDGE_QUOTE.format(token=token),
                                        headers={"Authorization": source.session_key},
                                        timeout=(8, 25))
            if response.status_code in (401, 403):
                with self._lock:
                    self._definedge_day = None
                return symbol, None
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return symbol, None
        if data.get("status") != "SUCCESS":
            return symbol, None
        price = _number(data.get("ltp"))
        if price <= 0:
            return symbol, None
        age = trade_age_seconds(data.get("last_trade_time"))
        # The REST quote has a clock time but no date, and after the close it keeps
        # serving a frozen mid-session snapshot (11:30 on the previous day, say).
        # Only a quote whose clock time is recent, during the session, is believable;
        # anything else is dropped so the daily close stands in instead.
        if age is None or age > MAX_DEFINEDGE_AGE or not market_open():
            return symbol, None
        close = _number(data.get("day_close")) or _number(data.get("prev_close"))
        return symbol, {
            "ltp": price,
            "change_pct": ((price - close) / close * 100) if close else None,
            "day_high": _number(data.get("day_high")) or None,
            "day_low": _number(data.get("day_low")) or None,
            "source": "Definedge",
            "quote_age_seconds": round(age, 1),
            "fetched_at": ist_now().isoformat(timespec="seconds"),
        }

    # -- public ------------------------------------------------------------ #
    def quotes(self, symbols) -> dict:
        """{symbol: quote} for as many as can be priced, plus a source note."""
        wanted = [s.upper() for s in dict.fromkeys(symbols) if s]
        if not wanted:
            return {"quotes": {}, "missing": [], "source": None, "market_open": market_open()}
        out: dict[str, dict] = {}
        errors: list[str] = []

        if time.monotonic() >= self._fyers_down_until:
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    chunks = [wanted[i:i + BATCH] for i in range(0, len(wanted), BATCH)]
                    for part in pool.map(self._fyers_batch, chunks):
                        out.update(part)
            except Exception as exc:
                errors.append(str(exc))
                self._fyers_down_until = time.monotonic() + COOLDOWN

        missing = [s for s in wanted if s not in out]
        if missing:
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    for symbol, quote in pool.map(self._definedge_one, missing[:120]):
                        if quote:
                            out[symbol] = quote
            except Exception as exc:
                errors.append(str(exc))

        sources = sorted({q["source"] for q in out.values()}) or None
        return {
            "quotes": out,
            "missing": [s for s in wanted if s not in out],
            "source": " + ".join(sources) if sources else None,
            "market_open": market_open(),
            "errors": errors[:2],
            "fetched_at": ist_now().isoformat(timespec="seconds"),
        }
