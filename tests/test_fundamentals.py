"""Lenders are judged on asset quality, not debt-to-equity."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ema5 import fundamentals as F  # noqa: E402


def page(*, borrow_label="Borrowings", sector="Financial Services Financial Services Finance Financial Institution",
         npa=None, net_profit=("4,034", "4,456")):
    """A screener.in page cut down to the parts the parser reads."""
    npa_rows = ""
    if npa is not None:
        gross, net = npa
        earlier = "1.00%" if gross else ""          # Jun column; Sep is the latest
        npa_rows = (f"<tr><td>Gross NPA %</td><td>{earlier}</td><td>{gross}</td></tr>"
                    f"<tr><td>Net NPA %</td><td>{earlier}</td><td>{net}</td></tr>")
    return f"""
    <h1>Test Co</h1>
    <ul id="top-ratios">
      <li><span class="name">Market Cap</span><span class="value">₹ 32,921 Cr.</span></li>
      <li><span class="name">Current Price</span><span class="value">₹ 164</span></li>
      <li><span class="name">Book Value</span><span class="value">₹ 115</span></li>
      <li><span class="name">ROCE</span><span class="value">8.41 %</span></li>
      <li><span class="name">ROE</span><span class="value">20.0 %</span></li>
    </ul>
    <section id="peers"><p class="sub">{sector}</p></section>
    <section id="quarters"><table><thead><tr><th></th><th>Jun 2026</th><th>Sep 2026</th></tr></thead>
      <tbody>{npa_rows}</tbody></table></section>
    <section id="balance-sheet"><table><thead><tr><th></th><th>Mar 2026</th><th>Sep 2026</th></tr></thead><tbody>
      <tr><td>Equity Capital</td><td>2,002</td><td>2,002</td></tr>
      <tr><td>Reserves</td><td>19,974</td><td>20,955</td></tr>
      <tr><td>{borrow_label}</td><td>141,677</td><td>165,159</td></tr>
      <tr><td>Total Assets</td><td>166,838</td><td>195,035</td></tr>
    </tbody></table></section>
    <section id="profit-loss"><table><thead><tr><th></th><th>Mar 2026</th><th>TTM</th></tr></thead><tbody>
      <tr><td>Net Profit+</td><td>{net_profit[0]}</td><td>{net_profit[1]}</td></tr>
    </tbody></table></section>"""


# ---- parsing ---------------------------------------------------------------
def test_lender_borrowing_row_is_found():
    """screener.in calls it 'Borrowing' for lenders; that was read as no figure."""
    out = F.parse_screener(page(borrow_label="Borrowing"), "u")
    assert out["debt_cr"] == 165159
    assert out["de"] is not None and round(out["de"], 2) == 7.19


def test_ordinary_company_borrowings_row_still_read():
    out = F.parse_screener(page(borrow_label="Borrowings+"), "u")
    assert out["debt_cr"] == 165159


def test_npa_read_from_latest_quarter_with_its_period():
    out = F.parse_screener(page(npa=("0.71%", "0.04%")), "u")
    assert out["gross_npa_pct"] == 0.71
    assert out["net_npa_pct"] == 0.04
    assert out["npa_period"] == "Sep 2026"


def test_blank_npa_cells_are_unknown_not_zero():
    out = F.parse_screener(page(npa=("", "")), "u")
    assert out["gross_npa_pct"] is None and out["net_npa_pct"] is None


def test_old_npa_figures_are_not_passed_off_as_current():
    """SBIN's consolidated page: NPAs for old quarters, blank since."""
    html = page(npa=("", "")).replace(
        "<tbody></tbody>", "")  # keep the fixture's skeleton
    html = html.replace('<th>Jun 2026</th><th>Sep 2026</th>',
                        '<th>Sep 2024</th><th>Dec 2024</th><th>Mar 2026</th><th>Jun 2026</th><th>Sep 2026</th>')
    html = html.replace("<tbody><tr><td>Gross NPA %</td><td></td><td></td></tr><tr><td>Net NPA %</td><td></td><td></td></tr></tbody>",
                        "<tbody><tr><td>Gross NPA %</td><td>2.13%</td><td>2.00%</td><td></td><td></td><td></td></tr>"
                        "<tr><td>Net NPA %</td><td>0.52%</td><td>0.50%</td><td></td><td></td><td></td></tr></tbody>")
    out = F.parse_screener(html, "u")
    assert out["net_npa_pct"] is None and out["gross_npa_pct"] is None


def test_previous_quarter_is_accepted_when_the_latest_is_blank():
    """LIC Housing standalone: newest quarter not yet published."""
    html = page(npa=("", "")).replace(
        "<tbody><tr><td>Gross NPA %</td><td></td><td></td></tr><tr><td>Net NPA %</td><td></td><td></td></tr></tbody>",
        "<tbody><tr><td>Gross NPA %</td><td>2.14%</td><td></td></tr>"
        "<tr><td>Net NPA %</td><td>1.12%</td><td></td></tr></tbody>")
    out = F.parse_screener(html, "u")
    assert out["net_npa_pct"] == 1.12 and out["npa_period"] == "Jun 2026"


def test_lender_figures_from_the_page():
    out = F.parse_screener(page(), "u")
    assert out["total_assets_cr"] == 195035
    assert out["net_profit_cr"] == 4456
    assert out["roe_pct"] == 20.0
    assert out["pb"] == 1.43          # 164 / 115


# ---- grading -----------------------------------------------------------------
def test_net_npa_grades():
    assert F.classify_lender(0.04) == "lender_clean"
    assert F.classify_lender(0.5) == "lender_clean"
    assert F.classify_lender(0.51) == "lender_sound"
    assert F.classify_lender(1.5) == "lender_sound"
    assert F.classify_lender(2.0) == "lender_watch"
    assert F.classify_lender(3.01) == "lender_stressed"


def test_no_net_npa_is_unknown_even_if_gross_is_published():
    assert F.classify_lender(None) == "unknown"


def test_every_verdict_has_a_label():
    for key in ("lender_clean", "lender_sound", "lender_watch", "lender_stressed", "unknown"):
        assert key in F.VERDICTS and key in F.ORDER


def test_kind_from_sector_line():
    assert F.lender_kind("Financial Services Financial Services Banks Private Sector Bank") == "Bank"
    assert F.lender_kind("Financial Services Financial Services Insurance Life Insurance") == "Insurer"
    assert F.lender_kind("Financial Services Financial Services Finance Housing Finance Company") == "NBFC"
    assert F.lender_kind("Financial Services Capital Markets Exchange and Data Platform") == "Other financial"


def test_profile_computes_leverage_and_roa_from_screener():
    scr = F.parse_screener(page(npa=("0.71%", "0.04%")), "u")
    profile = F.lender_profile(scr, "Finance")
    assert profile["leverage_x"] == 8.5             # 195,035 / 22,957 = 8.4958
    assert profile["roa_pct"] == 2.28               # 4,456 / 195,035
    assert profile["roe_pct"] == 20.0 and profile["pb"] == 1.43
    assert profile["net_npa_pct"] == 0.04 and profile["kind"] == "NBFC"


# ---- whole-record behaviour ----------------------------------------------------
def make_book(tmp_path, monkeypatch, nse, consolidated, standalone=None):
    book = F.DebtBook(tmp_path / "debt.json")
    calls = {"nse": 0}

    def marketlens(symbol):
        calls["nse"] += 1
        return nse

    monkeypatch.setattr(book, "_marketlens", marketlens)
    pages = {"/consolidated/": consolidated, "/": standalone}

    class Resp:
        def __init__(self, html, url):
            self.status_code, self.text, self.url = (200 if html else 404), html or "", url

    def fake_fetch(url, referer=None):
        key = "/consolidated/" if url.endswith("/consolidated/") else "/"
        return Resp(pages[key], url)

    monkeypatch.setattr(book, "_fetch", fake_fetch)
    book.calls = calls
    return book


def test_hudco_is_graded_on_net_npa_from_one_source(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "HUDCO", "sector": "Finance", "debtToEquity": 6.45})
    book = make_book(tmp_path, monkeypatch, nse,
                     consolidated=page(borrow_label="Borrowing", npa=("", "")),
                     standalone=page(borrow_label="Borrowing", npa=("0.71%", "0.04%")))
    record = book.refresh("HUDCO")
    assert record["financial"] is True
    assert record["verdict"] == "lender_clean"
    assert record["source"] == "screener.in"
    assert record["de"] is None                      # D/E is not used for a lender
    assert record["lender"]["net_npa_pct"] == 0.04
    assert record["lender"]["npa_basis"] == "standalone"
    assert book.calls["nse"] == 0                    # no second source consulted
    assert "de_nse" not in record and "de_screener" not in record


def test_consolidated_npa_is_used_without_a_second_page(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "PFC", "sector": "Finance"})
    book = make_book(tmp_path, monkeypatch, nse,
                     consolidated=page(borrow_label="Borrowing", npa=("0.66%", "0.13%")),
                     standalone=None)               # would 404 if asked for
    record = book.refresh("PFC")
    assert record["verdict"] == "lender_clean"
    assert record["lender"]["npa_basis"] == "consolidated"


def test_lender_without_net_npa_is_unknown_and_has_no_source(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "X", "sector": "Finance"})
    book = make_book(tmp_path, monkeypatch, nse,
                     consolidated=page(borrow_label="Borrowing", npa=("", "")),
                     standalone=page(borrow_label="Borrowing", npa=("", "")))
    record = book.refresh("X")
    assert record["verdict"] == "unknown" and record["source"] is None
    assert book.judge(record, 0.25, 1.0) == (None, "no net NPA data")


def test_ordinary_company_uses_screener_and_never_asks_nse(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "CROMPTON", "sector": "Consumer Durables",
                              "debtToEquity": 0.07, "roce": 20.0})
    book = make_book(tmp_path, monkeypatch, nse,
                     consolidated=page(sector="Consumer Discretionary Consumer Durables Consumer Electronics"))
    record = book.refresh("CROMPTON")
    assert record["financial"] is False and record["lender"] is None
    assert record["source"] == "screener.in"
    assert record["de"] == round(165159 / 22957, 6) or abs(record["de"] - 165159 / 22957) < 1e-9
    assert not record["verdict"].startswith("lender")
    assert book.calls["nse"] == 0


def test_nse_is_the_fallback_when_screener_has_no_figure(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "Y", "sector": "Chemicals", "debtToEquity": 0.30, "roce": 12.0,
                              "name": "Y Ltd"})
    book = make_book(tmp_path, monkeypatch, nse, consolidated=None, standalone=None)
    record = book.refresh("Y")
    assert record["source"] == "NSE MarketLens"
    assert record["de"] == 0.30 and record["verdict"] == "low"
    assert record["name"] == "Y Ltd"
    assert "screener.in" in " ".join(record["errors"])


def test_nothing_usable_anywhere_is_unknown(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "Z", "sector": "Chemicals"})   # all zeros = no data
    book = make_book(tmp_path, monkeypatch, nse, consolidated=None, standalone=None)
    record = book.refresh("Z")
    assert record["verdict"] == "unknown" and record["source"] is None and record["de"] is None
    assert book.judge(record, 0.25, 1.0) == (None, "no debt data")


def test_a_lender_found_only_by_nse_is_unknown_not_scored(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "B", "sector": "Banks", "debtToEquity": 0.9, "roce": 7.0})
    book = make_book(tmp_path, monkeypatch, nse, consolidated=None, standalone=None)
    record = book.refresh("B")
    assert record["financial"] is True and record["verdict"] == "unknown"


# ---- the filter ---------------------------------------------------------------
def test_judge_uses_de_for_companies_and_net_npa_for_lenders():
    book = F.DebtBook(Path("/nonexistent/debt.json"))
    company = {"financial": False, "verdict": "low", "de": 0.20}
    assert book.judge(company, 0.25, 1.0) == (True, "D/E 0.20")
    assert book.judge({**company, "de": 0.55}, 0.25, 1.0) == (False, "D/E 0.55")
    lender = {"financial": True, "verdict": "lender_sound", "de": None,
              "lender": {"net_npa_pct": 0.9}}
    assert book.judge(lender, 0.25, 1.0) == (True, "net NPA 0.90%")
    assert book.judge(lender, 0.25, 0.5) == (False, "net NPA 0.90%")
    assert book.judge(None, 0.25, 1.0) == (None, "not checked yet")
    # a graded lender is never measured against the D/E ceiling
    assert book.judge({**lender, "de": 7.0}, 0.25, 1.0)[0] is True


def test_record_in_an_older_shape_is_refetched(tmp_path):
    import json, time
    path = tmp_path / "debt.json"
    path.write_text(json.dumps({"companies": {
        "OLD": {"symbol": "OLD", "verdict": "zero", "de": 0.0, "fetched_at_ts": time.time()},
        "NEW": {"v": F.RECORD_VERSION, "symbol": "NEW", "verdict": "zero", "de": 0.0,
                "fetched_at_ts": time.time()}}}))
    book = F.DebtBook(path)
    assert book.cached("OLD")["stale"] is True
    assert book.cached("NEW")["stale"] is False


def test_a_known_lender_is_not_refetched_on_every_scan(tmp_path, monkeypatch):
    nse = F.parse_marketlens({"ticker": "PFC", "sector": "Finance"})
    book = make_book(tmp_path, monkeypatch, nse,
                     consolidated=page(borrow_label="Borrowing", npa=("0.66%", "0.13%")))
    first = book.get("PFC")
    monkeypatch.setattr(book, "refresh", lambda s: (_ for _ in ()).throw(AssertionError("refetched")))
    assert book.get("PFC")["verdict"] == first["verdict"]
