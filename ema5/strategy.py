"""The 5 EMA monthly strategy as pure functions over daily OHLC frames.

Rules, straight from `5 EMA strategy_monthly.pdf` and `amibroker_5ema.txt`:

  Alert candle  a COMPLETED monthly candle whose high is strictly below the
                5 EMA of monthly closes. A candle whose high touches or
                exceeds the EMA is ignored -- it neither becomes an alert
                candle nor cancels the one in force. The alert candle is
                therefore the most recent completed month with high < 5 EMA.
  Entry         a daily high crossing  alert high x (1 + buffer%).
  Stop          the alert candle low. Risk R = entry - stop.
  Trail         at 2R the stop moves to breakeven, at 3R to entry + 1R, and
                from 4R upward it follows continuously 2R behind the peak.
                The stop only ever ratchets up.

Nothing here talks to a broker, so every rule is unit-testable.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

EMA_SPAN = 5
DEFAULT_BUFFER_PCT = 0.1
# A 5 EMA seeded from the first close is accurate to ~1e-4 after 15 months.
# Below this the signal is not trustworthy, so the stock is skipped.
MIN_COMPLETED_MONTHS = 15

STATUS_NO_DATA = "no_data"
STATUS_SHORT_HISTORY = "short_history"
STATUS_NO_ALERT = "no_alert"
STATUS_PENDING = "pending"
STATUS_TRIGGERED = "triggered"


# --------------------------------------------------------------------------- #
# monthly bars
# --------------------------------------------------------------------------- #
def to_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """Calendar-month OHLCV from daily bars.

    Each monthly bar is stamped with its LAST trading day, so the current,
    still-forming month appears with the latest session's date. `reduceat`
    rather than `groupby`: this runs over ~1500 symbols per scan.
    """
    if daily is None or daily.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], name="date"), dtype=float)
    index = pd.DatetimeIndex(daily.index)
    key = index.year.to_numpy() * 12 + index.month.to_numpy()
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(key)] - 1
    o, h, l, c, v = (daily[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    return pd.DataFrame(
        {
            "open": o[starts],
            "high": np.maximum.reduceat(h, starts),
            "low": np.minimum.reduceat(l, starts),
            "close": c[ends],
            "volume": np.add.reduceat(v, starts),
        },
        index=pd.DatetimeIndex(index.values[ends], name="date"),
    )


def monthly_ema(monthly: pd.DataFrame, span: int = EMA_SPAN) -> pd.Series:
    """Recursive EMA of monthly closes -- the same series Amibroker's EMA(C,5) gives."""
    return monthly["close"].ewm(span=span, adjust=False).mean()


def completed(monthly: pd.DataFrame, asof: date | None = None) -> pd.DataFrame:
    """Drop the month in progress. The rules act only on completed candles."""
    if monthly.empty:
        return monthly
    asof = asof or date.today()
    last = monthly.index[-1]
    if (last.year, last.month) == (asof.year, asof.month):
        return monthly.iloc[:-1]
    return monthly


# --------------------------------------------------------------------------- #
# the setup
# --------------------------------------------------------------------------- #
def trigger_price(alert_high: float, buffer_pct: float = DEFAULT_BUFFER_PCT) -> float:
    return alert_high * (1 + buffer_pct / 100)


def evaluate(daily: pd.DataFrame, buffer_pct: float = DEFAULT_BUFFER_PCT,
             asof: date | None = None) -> dict:
    """Current state of the 5 EMA setup for one stock.

    Returns a JSON-ready dict; `status` says how far the setup has got:
      no_data / short_history / no_alert  -- nothing to watch
      pending    -- an alert candle is in force and price has not crossed it
      triggered  -- price already crossed the trigger after the alert month
    """
    out: dict = {"status": STATUS_NO_DATA}
    if daily is None or daily.empty:
        return out

    monthly = to_monthly(daily)
    done = completed(monthly, asof)
    if len(done) < MIN_COMPLETED_MONTHS:
        return {"status": STATUS_SHORT_HISTORY, "months": len(done)}

    ema = monthly_ema(done)
    below = done["high"].to_numpy(float) < ema.to_numpy(float)
    found = np.flatnonzero(below)
    if not len(found):
        return {"status": STATUS_NO_ALERT}

    pos = int(found[-1])
    stamp = done.index[pos]
    alert_high = float(done["high"].iloc[pos])
    alert_low = float(done["low"].iloc[pos])
    trigger = trigger_price(alert_high, buffer_pct)
    last_close = float(daily["close"].iloc[-1])

    # Days after the alert month closed. A high at or above the trigger means
    # the entry has already fired, so the setup is no longer something to watch.
    after = daily[daily.index > stamp]
    fired = after[after["high"].to_numpy(float) >= trigger] if len(after) else after
    out = {
        "status": STATUS_TRIGGERED if len(fired) else STATUS_PENDING,
        "alert_month": f"{stamp.year:04d}-{stamp.month:02d}",
        "alert_month_end": stamp.date().isoformat(),
        "alert_high": alert_high,
        "alert_low": alert_low,
        "alert_close": float(done["close"].iloc[pos]),
        "alert_open": float(done["open"].iloc[pos]),
        "ema5": float(ema.iloc[pos]),
        "ema5_latest": float(monthly_ema(monthly).iloc[-1]),
        "trigger": trigger,
        "stop": alert_low,
        "risk": trigger - alert_low,
        "risk_pct": (trigger - alert_low) / trigger * 100 if trigger else None,
        "last_close": last_close,
        "last_bar": daily.index[-1].date().isoformat(),
        "months_of_history": len(done),
        "months_since_alert": len(done) - 1 - pos,
        "buffer_pct": buffer_pct,
    }
    if len(fired):
        row = fired.iloc[0]
        out["triggered_on"] = fired.index[0].date().isoformat()
        out["triggered_high"] = float(row["high"])
    out["distance_pct"] = distance_pct(trigger, last_close)
    return out


def distance_pct(trigger: float, price: float) -> float | None:
    """How far price still has to rise to reach the trigger, in percent.
    Negative once price is above the trigger."""
    if not price or price <= 0:
        return None
    return (trigger - price) / price * 100


def monthly_series(daily: pd.DataFrame, months: int = 30, asof: date | None = None) -> list[dict]:
    """Recent monthly candles with their 5 EMA, for the detail chart."""
    monthly = to_monthly(daily)
    if monthly.empty:
        return []
    ema = monthly_ema(monthly)
    asof = asof or date.today()
    rows = []
    for stamp, row in monthly.tail(months).iterrows():
        forming = (stamp.year, stamp.month) == (asof.year, asof.month)
        rows.append({
            "month": f"{stamp.year:04d}-{stamp.month:02d}",
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "ema5": float(ema.loc[stamp]) if np.isfinite(ema.loc[stamp]) else None,
            "forming": bool(forming),
        })
    return rows


# --------------------------------------------------------------------------- #
# the trailing stop
# --------------------------------------------------------------------------- #
def trail_stop(entry: float, stop: float, peak_r: float) -> float:
    """Stop in force once the trade's best R multiple is `peak_r`.

    2R -> breakeven, 3R -> entry + 1R, 4R and beyond -> 2R behind the peak.
    Never below the original stop, and never moving down.
    """
    risk = entry - stop
    if risk <= 0:
        return stop
    level = stop
    if peak_r >= 2:
        level = max(level, entry)
    if peak_r >= 3:
        level = max(level, entry + risk)
    if peak_r >= 4:
        level = max(level, entry + (peak_r - 2) * risk)
    return level


def ladder(entry: float, stop: float, steps: int = 6) -> list[dict]:
    """The published 2R/3R/4R... table, for display next to a live trade."""
    risk = entry - stop
    return [{"r": r, "target": entry + r * risk, "stop": trail_stop(entry, stop, r),
             "locked_r": (trail_stop(entry, stop, r) - entry) / risk if risk else 0.0}
            for r in range(2, 2 + steps)]


def track(daily: pd.DataFrame, entry: float, stop: float, entry_date: date) -> dict:
    """Replay the trade day by day from `entry_date` to now.

    Within a single day the stop in force at the open is tested against that
    day's low BEFORE the day's high is allowed to raise it, so the stop can
    never be lifted by the same bar that breaks it. A day that opens below
    the stop fills at the open, not at the stop.
    """
    risk = entry - stop
    result = {
        "entry": entry, "stop_initial": stop, "risk": risk,
        "peak_r": 0.0, "stop_now": stop, "locked_r": (stop - entry) / risk if risk else 0.0,
        "bars": 0, "exit": None, "peak_price": None, "peak_on": None,
    }
    if risk <= 0 or daily is None or daily.empty:
        return result

    after = daily[daily.index >= pd.Timestamp(entry_date)]
    if after.empty:
        return result

    peak_r, level, peak_price, peak_on = 0.0, stop, None, None
    highs = after["high"].to_numpy(float)
    lows = after["low"].to_numpy(float)
    opens = after["open"].to_numpy(float)
    for i in range(len(after)):
        if lows[i] <= level:
            fill = min(level, opens[i])
            result["exit"] = {
                "date": after.index[i].date().isoformat(),
                "price": fill,
                "r": (fill - entry) / risk,
                "gap": bool(opens[i] < level),
                "stop_level": level,
            }
            break
        if peak_price is None or highs[i] > peak_price:
            peak_price, peak_on = highs[i], after.index[i].date().isoformat()
        peak_r = max(peak_r, (highs[i] - entry) / risk)
        level = trail_stop(entry, stop, peak_r)
        result["bars"] = i + 1

    result.update(peak_r=peak_r, stop_now=level, peak_price=peak_price, peak_on=peak_on,
                  locked_r=(level - entry) / risk)
    return result
