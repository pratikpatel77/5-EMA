# 5 EMA Monthly Desk

A local dashboard for the 5 EMA monthly strategy: it finds the stocks sitting
just under their entry trigger, checks how much debt they carry, and then
tracks the ones you take through the trailing-stop ladder.

Nothing here places an order. "Take entry" means *track this*.

```bash
python -m ema5
```

Then open http://127.0.0.1:8777 and press **Run scan**.

---

## What it does

**1. Screens for the setup.** For every stock in the universe it builds monthly
candles from daily bars, takes the 5 EMA of monthly closes, and finds the most
recent *completed* month whose high is below that EMA — the alert candle. The
trigger is the alert high plus a buffer (0.1% by default); the stop is the alert
low. A month whose high touches or clears the EMA is ignored: it neither becomes
an alert candle nor cancels the one in force.

**2. Shows only what is close.** The main list holds stocks between **0% and 2%**
below the trigger. A stock that has already crossed its trigger drops off — that
entry has happened. A second, wider list ("Approaching", 6% by default) gives you
some warning. Both bands are adjustable.

**3. Checks the debt.** Only when a stock reaches the narrow band, so a normal
day is a handful of lookups. Two sources, cached for a week:

| Source | What it gives |
|---|---|
| [NSE MarketLens](https://marketlens.nseindia.com/screener) | `debtToEquity` straight from NSE, plus interest cover, ROCE, promoter holding |
| [screener.in](https://www.screener.in) | Borrowings ÷ (Equity Capital + Reserves) off the balance sheet |

The **stricter of the two** decides the verdict, so nothing is called debt-free on
the friendlier reading alone. They legitimately differ: screener.in counts lease
liabilities as borrowings, so it reads AstraZeneca at 0.11 where NSE reads 0.00.
If screener.in has no figure, NSE alone decides, and the drawer says so.

Banks, NBFCs and insurers are **flagged, never scored** — borrowings are a
lender's raw material, not leverage. MarketLens also returns a flat zero for
every ratio it has no data for, which is read as *unknown*, not as *no debt*.

**4. Tracks what you take.** The entry defaults to the trigger price and you can
replace it with your actual fill. From there it replays the trade day by day:

| Peak reached | Stop moves to |
|---|---|
| under 2R | the alert candle low |
| 2R | breakeven |
| 3R | entry + 1R |
| 4R and beyond | 2R behind the peak, continuously |

The stop only ever ratchets up. Within a day the stop in force at the open is
tested against that day's low *before* the day's high is allowed to raise it, so
a bar can never be stopped out by a level its own high created. A day that opens
below the stop fills at the open, not at the stop.

> **One thing to know:** the PDF's example table says the stop is +9R at 10R.
> Its own step-by-step rule ("At 1:4, trail to 2R; At 1:5, trail to 3R") and
> your Amibroker formula both give +8R. The code follows the rule and the
> formula; the 10R row looks like a typo. It only matters past 4R.

---

## Data

Prices come from the **pattern-scanners** project next door, so there is one set
of logins and one shared bar cache:

- **Definedge** for bulk daily history — logs in by itself with TOTP.
- **Fyers** for live prices, with Definedge as the fallback when the daily Fyers
  token has expired. (Refresh it with `python tools/fyers_login.py` over there.)

The universe is the top N by market cap (1,500 by default) from
`pattern-scanners/universe/nse_by_market_cap.csv`.

If that project ever moves, point the app at it:

```bash
set EMA5_SCANNERS_DIR=D:\path\to\pattern-scanners
```

A full scan is minutes on a cold cache and seconds once warm. Live prices for the
rows on screen refresh by themselves every 25 seconds while the market is open.

**Reopening.** Every scan is saved to `.cache/scan.pkl`. On the next start the desk
opens on it straight away, so candidates and open trades are on screen in under a
second. It then puts live prices on those rows and runs a fresh scan in the
background; the header says "saved scan from ..." until that finishes. If the fresh
scan fails (a login problem, say) the saved results stay, with their original date.
After a month rollover the saved alert candles are a month out of date, and the
page tells you so until the fresh scan replaces them.

**Prices.** Fyers answers first. Definedge is the fallback, but its REST quote has
no date and, after the close, keeps serving a frozen mid-session snapshot, so it is
only used during the session and only when its clock time is under five minutes
old. Otherwise the last daily close stands in.

---

## The screen

- **At the trigger** — the 0–2% band. Take or ignore each one.
- **Approaching** — the wider band, so nothing arrives unannounced.
- **Open trades** — live R multiple, where the stop now sits, what is locked in,
  and the next rung.
- **Closed** — stopped out or an exit you recorded, with the R result.
- **Ignored** — skipped for *that* alert candle. A fresh alert candle on the same
  stock brings it back by itself.

Click any row for the monthly candle chart, the 5 EMA, both debt sources and the
full ladder.

---

## Settings

Entry band, approaching band, entry buffer, universe size, debt-to-equity ceiling,
price history, data source, and an optional automatic re-scan (off by default;
only runs while the market is open).

---

## Layout

```
ema5/
  strategy.py      the rules -- pure functions, no network, fully tested
  service.py       scanning, tracking, the payload the page renders
  fundamentals.py  the two debt sources, cached
  quotes.py        live prices (Fyers, then Definedge)
  store.py         settings and tracked entries (.cache/state.json)
  bridge.py        finds the pattern-scanners data layer
  server.py        loopback-only HTTP server
  static/          the dashboard
tests/             29 tests over the rules
```

The server binds to 127.0.0.1, refuses any other Host or Origin, and requires a
per-process token for every write. No credentials reach the browser.

```bash
python -m pytest tests -q
```

## Options

```
python -m ema5 --port 8777     a different port
python -m ema5 --no-scan       open on the saved scan, skip the fresh one
python -m ema5 --offline       use only cached bars, no broker calls
python -m ema5 --no-open       don't open a browser
```
