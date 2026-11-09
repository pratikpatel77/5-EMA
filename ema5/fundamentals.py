"""How much debt a candidate carries, from ONE source per stock.

Looked up for every stock in the approaching band (so by the time one reaches
the trigger it is already known), cached for a week -- debt moves with
quarterly results, not with price -- and spaced out one request at a time.

  1. screener.in  https://www.screener.in/company/<SYMBOL>/consolidated/
         Read off the balance sheet: D/E = Borrowings / (Equity Capital +
         Reserves). Lenders are read off it too (NPAs, ROA, ROE, P/B).
  2. NSE MarketLens  https://marketlens.nseindia.com/api/stocks/<SYMBOL>
         Only when screener.in has no figure for an ordinary company.

Whichever source answered is the only one stored and shown. If neither has a
usable figure the stock is "unknown" and the desk skips it: no guessing.

Banks, NBFCs and insurers are NOT scored on debt-to-equity: on their balance
sheet borrowings and deposits are raw material, not leverage. They are judged
on Net NPA % (bad loans left after provisions) and shown with Gross NPA,
assets per rupee of equity, ROA, ROE and price to book. screener.in often
leaves the NPA rows blank on the consolidated page and fills them on the
standalone one, so the standalone page is tried when needed. A lender with no
Net NPA figure is unknown, and skipped.

MarketLens returns a flat zero for every ratio it has no data for, which must
not be read as "no debt", so a row of zeros counts as unknown.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

MARKETLENS = "https://marketlens.nseindia.com"
SCREENER = "https://www.screener.in"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
MIN_GAP_SECONDS = 1.5
TIMEOUT = (10, 25)

FINANCIAL_WORDS = ("bank", "finance", "financial services", "nbfc", "insurance",
                   "capital market", "asset management", "fintech", "holding compan")

ZERO_DEBT_DE = 0.02
VERY_LOW_DE = 0.10
LOW_DE = 0.35
MODERATE_DE = 1.00

# Lenders are graded on Net NPA % (bad loans after provisions).
CLEAN_NNPA, SOUND_NNPA, WATCH_NNPA = 0.5, 1.5, 3.0
NPA_FRESH_QUARTERS = 2     # an NPA older than this many quarters is not current
RECORD_VERSION = 2         # bumped when the saved record's shape changes

VERDICTS = {
    "zero": ("Zero debt", "Carries no borrowings."),
    "very_low": ("Very low debt", "Borrowings under a tenth of net worth."),
    "low": ("Low debt", "Comfortably geared."),
    "moderate": ("Moderate debt", "Borrowings up to net worth."),
    "high": ("High debt", "Borrowings above net worth."),
    "lender_clean": ("Clean book", "Lender: net NPA at or below 0.5%."),
    "lender_sound": ("Sound book", "Lender: net NPA up to 1.5%."),
    "lender_watch": ("Watch NPAs", "Lender: net NPA between 1.5% and 3%."),
    "lender_stressed": ("Stressed book", "Lender: net NPA above 3%."),
    "unknown": ("Debt unknown", "No usable figure from screener.in or NSE MarketLens."),
}
ORDER = ["zero", "very_low", "low", "moderate", "high",
         "lender_clean", "lender_sound", "lender_watch", "lender_stressed", "unknown"]


def _number(text) -> float | None:
    if isinstance(text, (int, float)):
        return float(text) if text == text else None
    text = (text or "").replace(",", "").replace("%", "").replace("₹", "").strip()
    if text in ("", "-", "--"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def is_financial(sector: str | None) -> bool:
    return any(word in (sector or "").lower() for word in FINANCIAL_WORDS)


def lender_kind(sector: str | None) -> str:
    text = (sector or "").lower()
    if "bank" in text:
        return "Bank"
    if "insurance" in text:
        return "Insurer"
    if any(word in text for word in ("capital market", "asset management", "exchange",
                                     "fintech", "holding compan")):
        return "Other financial"
    return "NBFC"


def classify_lender(net_npa: float | None) -> str:
    """Asset-quality grade from Net NPA %; unknown when it is not published."""
    if net_npa is None:
        return "unknown"
    if net_npa <= CLEAN_NNPA:
        return "lender_clean"
    if net_npa <= SOUND_NNPA:
        return "lender_sound"
    if net_npa <= WATCH_NNPA:
        return "lender_watch"
    return "lender_stressed"


def _ratio(top, bottom, scale: float = 1.0) -> float | None:
    if top is None or not bottom or bottom <= 0:
        return None
    return round(top / bottom * scale, 2)


def lender_profile(scr: dict, sector: str | None) -> dict:
    """The ratios a bank / NBFC / insurer is judged by, all from screener.in."""
    assets, networth = scr.get("total_assets_cr"), scr.get("networth_cr")
    has_npa = scr.get("net_npa_pct") is not None or scr.get("gross_npa_pct") is not None
    return {
        "kind": lender_kind(sector),
        "net_npa_pct": scr.get("net_npa_pct"),
        "gross_npa_pct": scr.get("gross_npa_pct"),
        "npa_period": scr.get("npa_period"),
        "npa_basis": scr.get("npa_basis") if has_npa else None,
        "leverage_x": _ratio(assets, networth),
        "roa_pct": _ratio(scr.get("net_profit_cr"), assets, 100),
        "roe_pct": scr.get("roe_pct"),
        "pb": scr.get("pb"),
    }


def classify(de: float | None) -> str:
    if de is None:
        return "unknown"
    if de <= ZERO_DEBT_DE:
        return "zero"
    if de <= VERY_LOW_DE:
        return "very_low"
    if de <= LOW_DE:
        return "low"
    if de <= MODERATE_DE:
        return "moderate"
    return "high"


# --------------------------------------------------------------------------- #
# the two sources
# --------------------------------------------------------------------------- #
def parse_marketlens(data: dict) -> dict:
    sector = data.get("sector") or data.get("subSector")
    de = _number(data.get("debtToEquity"))
    debt = _number(data.get("totalDebt"))
    cover = _number(data.get("interestCover"))
    roce = _number(data.get("roce"))
    # Every ratio flat zero means "not supplied", not "no debt at all".
    blank = all(not v for v in (de, debt, cover, roce))
    return {
        "source": "NSE MarketLens",
        "name": data.get("name"),
        "de": None if blank else de,
        "debt_cr": (debt / 1e7) if debt else (0.0 if de == 0 and not blank else None),
        "interest_cover": cover or None,
        "roce_pct": roce or None,
        "sector": sector,
        "market_cap_cr": (_number(data.get("marketCap")) or 0) / 1e7 or None,
        "promoter_pct": _number(data.get("promoterHolding")),
        "current_ratio": _number(data.get("currentRatio")),
        "as_of": data.get("lastUpdated"),
        "url": f"{MARKETLENS}/stocks/{quote(str(data.get('ticker') or ''))}",
        "no_data": blank,
    }


def _table(soup, section: str) -> tuple[list[str], dict[str, list[str]]]:
    """Column headings and {row label: cells} for one screener.in section."""
    table = soup.select_one(f"section#{section} table")
    if not table:
        return [], {}
    heads = [th.get_text(strip=True) for th in table.select("thead th")]
    rows: dict[str, list[str]] = {}
    for tr in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.select("td")]
        if cells:
            rows[cells[0].rstrip("+").strip()] = cells
    return heads, rows


def _latest(cells: list[str], heads: list[str] | None = None,
            within: int | None = None) -> tuple[float | None, str | None]:
    """Right-most numeric cell of a row, and the column heading above it.

    `within` limits the look-back to that many columns, so an old figure is
    never passed off as the current one."""
    stop = 0 if within is None else max(0, len(cells) - 1 - within)
    for i in range(len(cells) - 1, stop, -1):
        value = _number(cells[i])
        if value is not None:
            return value, (heads[i] if heads and i < len(heads) else None)
    return None, None


def parse_screener(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ratios: dict[str, str] = {}
    for item in soup.select("#top-ratios li"):
        label, value = item.select_one(".name"), item.select_one(".value")
        if label and value:
            ratios[label.get_text(" ", strip=True)] = value.get_text(" ", strip=True)

    peers = soup.select_one("#peers p.sub, #peers .sub")
    sector = (peers.get_text(" ", strip=True)[:120] if peers else None)

    # Some companies file no consolidated accounts, and screener.in then
    # serves the table with its label column only -- hence "last cell that
    # is actually a number" rather than simply the last cell.
    heads, bs = _table(soup, "balance-sheet")
    period = next((h for h in reversed(heads) if h), None)
    rows = {label: _latest(cells)[0] for label, cells in bs.items()}

    # Lenders label the row "Borrowing", everyone else "Borrowings".
    debt = rows.get("Borrowings") if rows.get("Borrowings") is not None else rows.get("Borrowing")
    networth = (rows.get("Equity Capital") or 0.0) + (rows.get("Reserves") or 0.0)
    de = debt / networth if debt is not None and networth > 0 else None

    # Lender figures: NPAs come from the quarterly table, profit from the P&L.
    q_heads, quarters = _table(soup, "quarters")
    # Only the latest two quarters count: some consolidated pages carry NPAs
    # for old quarters and nothing since, and that must not read as current.
    gross, gross_period = _latest(quarters.get("Gross NPA %", []), q_heads, within=NPA_FRESH_QUARTERS)
    net, net_period = _latest(quarters.get("Net NPA %", []), q_heads, within=NPA_FRESH_QUARTERS)
    _, pl = _table(soup, "profit-loss")
    profit = _latest(pl.get("Net Profit", []))[0]
    price, book = _number(ratios.get("Current Price", "")), _number(ratios.get("Book Value", ""))

    name = soup.select_one("h1")
    return {
        "source": "screener.in",
        "name": name.get_text(strip=True) if name else None,
        "de": de,
        "debt_cr": debt,
        "networth_cr": networth or None,
        "total_assets_cr": rows.get("Total Assets"),
        "net_profit_cr": profit,
        "gross_npa_pct": gross,
        "net_npa_pct": net,
        "npa_period": net_period or gross_period,
        "roe_pct": _number(ratios.get("ROE", "")),
        "pb": _ratio(price, book),
        "roce_pct": _number(ratios.get("ROCE", "")),
        "market_cap_cr": _number((ratios.get("Market Cap") or "").replace("Cr.", "")),
        "sector": sector,
        "period": period,
        "url": url,
        "no_data": debt is None,
    }


# --------------------------------------------------------------------------- #
class DebtBook:
    """Cached debt lookups, one network request at a time."""

    def __init__(self, path: Path, ttl_days: float = 7.0):
        self.path = Path(path)
        self.ttl = ttl_days * 86400
        self._lock = threading.RLock()
        self._net = threading.Lock()
        self._last_call = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        })
        self._seeded = False
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text("utf-8")).get("companies", {})
            except (OSError, ValueError):
                self._data = {}

    # -- cache ------------------------------------------------------------- #
    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"companies": self._data}, indent=1), "utf-8")
        tmp.replace(self.path)

    def cached(self, symbol: str) -> dict | None:
        with self._lock:
            entry = self._data.get(symbol.upper())
        if not entry:
            return None
        stale = time.time() - entry.get("fetched_at_ts", 0) >= self.ttl
        # A record saved in an older shape is refetched on next use.
        if entry.get("v") != RECORD_VERSION:
            stale = True
        return {**entry, "stale": stale}

    def known(self, symbols) -> dict[str, dict]:
        out = {}
        for symbol in symbols:
            entry = self.cached(symbol)
            if entry:
                out[symbol.upper()] = entry
        return out

    # -- network ----------------------------------------------------------- #
    def _fetch(self, url: str, referer: str | None = None):
        with self._net:
            gap = MIN_GAP_SECONDS - (time.monotonic() - self._last_call)
            if gap > 0:
                time.sleep(gap)
            self._last_call = time.monotonic()
            headers = {"Referer": referer} if referer else None
            return self._session.get(url, headers=headers, timeout=TIMEOUT)

    def _marketlens(self, symbol: str) -> dict:
        if not self._seeded:
            try:  # one page view so the session looks like a normal visit
                self._fetch(f"{MARKETLENS}/screener")
            except requests.RequestException:
                pass
            self._seeded = True
        try:
            response = self._fetch(f"{MARKETLENS}/api/stocks/{quote(symbol)}",
                                   referer=f"{MARKETLENS}/screener")
        except requests.RequestException as exc:
            return {"source": "NSE MarketLens", "error": f"unreachable ({type(exc).__name__})"}
        if response.status_code == 404:
            return {"source": "NSE MarketLens", "error": "not listed on MarketLens"}
        if response.status_code != 200:
            return {"source": "NSE MarketLens", "error": f"HTTP {response.status_code}"}
        try:
            payload = response.json()
        except ValueError:
            return {"source": "NSE MarketLens", "error": "unreadable response"}
        data = payload.get("data") or {}
        if not data:
            return {"source": "NSE MarketLens", "error": "no data returned"}
        return parse_marketlens({"ticker": symbol, **data})

    def _screener(self, symbol: str) -> dict:
        """Consolidated first; fall through to standalone when it has no figures."""
        best: dict | None = None
        for path in (f"/company/{quote(symbol)}/consolidated/", f"/company/{quote(symbol)}/"):
            try:
                response = self._fetch(SCREENER + path)
            except requests.RequestException as exc:
                return best or {"source": "screener.in", "error": f"unreachable ({type(exc).__name__})"}
            if response.status_code == 200:
                try:
                    parsed = parse_screener(response.text, str(response.url))
                except Exception:
                    return best or {"source": "screener.in", "error": "could not read the balance sheet"}
                parsed["npa_basis"] = "consolidated" if "consolidated" in path else "standalone"
                if parsed.get("de") is not None:
                    return self._with_npa(symbol, parsed)
                best = best or parsed  # keep the name and sector at least
                continue
            if response.status_code == 429:
                return best or {"source": "screener.in", "error": "rate limited"}
            if response.status_code != 404:
                return best or {"source": "screener.in", "error": f"HTTP {response.status_code}"}
        if best is not None:
            best.setdefault("error", "no borrowings figure on either statement")
            return best
        return {"source": "screener.in", "error": "no company page found"}

    def _with_npa(self, symbol: str, parsed: dict) -> dict:
        """screener.in often leaves NPAs blank on the consolidated page of a
        lender but fills them on the standalone one."""
        if (not is_financial(parsed.get("sector")) or parsed["npa_basis"] != "consolidated"
                or parsed.get("net_npa_pct") is not None or parsed.get("gross_npa_pct") is not None):
            return parsed
        try:
            response = self._fetch(f"{SCREENER}/company/{quote(symbol)}/")
            if response.status_code != 200:
                return parsed
            alt = parse_screener(response.text, str(response.url))
        except Exception:
            return parsed
        if alt.get("net_npa_pct") is not None or alt.get("gross_npa_pct") is not None:
            parsed.update({k: alt.get(k) for k in ("net_npa_pct", "gross_npa_pct", "npa_period")},
                          npa_basis="standalone")
        return parsed

    # -- combine ----------------------------------------------------------- #
    def refresh(self, symbol: str) -> dict:
        symbol = symbol.upper()
        scr = self._screener(symbol)
        sector = scr.get("sector")
        financial = is_financial(sector)
        nse: dict = {}
        source: dict | None = scr if scr.get("de") is not None else None

        if source is None and not financial:
            # screener.in had nothing: NSE MarketLens is the one fallback.
            nse = self._marketlens(symbol)
            sector = sector or nse.get("sector")
            if is_financial(nse.get("sector")):
                financial = True        # a lender, and only screener.in has NPAs
            elif nse.get("de") is not None and not nse.get("no_data"):
                source = nse

        lender = lender_profile(scr, sector) if financial else None
        if financial:
            verdict = classify_lender(lender["net_npa_pct"])
            source = scr if verdict != "unknown" else None
        else:
            verdict = classify(source["de"]) if source else "unknown"

        record = {
            "v": RECORD_VERSION,
            "symbol": symbol,
            "name": scr.get("name") or nse.get("name") or None,
            "sector": sector,
            "financial": financial,
            "lender": lender,
            "source": source["source"] if source else None,
            "source_url": source.get("url") if source else None,
            "de": None if financial or not source else source["de"],
            "debt_cr": source.get("debt_cr") if source else None,
            "networth_cr": source.get("networth_cr") if source else None,
            "roce_pct": source.get("roce_pct") if source else None,
            "market_cap_cr": source.get("market_cap_cr") if source else None,
            "period": (source.get("period") or source.get("as_of")) if source else None,
            "verdict": verdict,
            "label": VERDICTS[verdict][0],
            "errors": [f"{v['source']}: {v['error']}" for v in (scr, nse) if v.get("error")],
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "fetched_at_ts": time.time(),
        }
        with self._lock:
            self._data[symbol] = record
            self._save()
        return {**record, "stale": False}

    def get(self, symbol: str, refresh: bool = False) -> dict:
        if not refresh:
            entry = self.cached(symbol)
            if entry and not entry["stale"] and entry.get("verdict") != "unknown":
                return entry
            # A lookup that just failed is not retried on every scan.
            if entry and entry.get("verdict") == "unknown" and entry.get("v") == RECORD_VERSION \
                    and time.time() - entry.get("fetched_at_ts", 0) < 1800:
                return entry
        return self.refresh(symbol)

    def judge(self, record: dict | None, max_de: float, max_nnpa: float) -> tuple[bool | None, str]:
        """(passes, why). passes is None when the figure is not known, and an
        unknown stock is skipped, never waved through."""
        if not record:
            return None, "not checked yet"
        if record.get("financial"):
            npa = (record.get("lender") or {}).get("net_npa_pct")
            if npa is None:
                return None, "no net NPA data"
            return npa <= max_nnpa, f"net NPA {npa:.2f}%"
        de = record.get("de")
        if record.get("verdict") == "unknown" or de is None:
            return None, "no debt data"
        return de <= max_de, f"D/E {de:.2f}"
