import datetime as dt

import pytest

from paper_public import MockMarketData, PaperBroker
from paper_public import portfolio as pf
from paper_public.broker import ET
from paper_public.instruments import option_symbol

EXP = dt.date(2026, 10, 16)
C = lambda k: option_symbol("XYZ", EXP, "C", k)   # noqa: E731
P = lambda k: option_symbol("XYZ", EXP, "P", k)   # noqa: E731


@pytest.fixture
def b(tmp_path):
    md = MockMarketData()
    md.set("XYZ", 99.99, 100.01, last=100.0)
    md._q["XYZ"].previous_close = 98.0
    for k, bid, ask in [(95, 1.1, 1.15), (100, 2.9, 3.0), (105, 1.2, 1.25)]:
        md.set(P(k), bid, ask); md.set(C(k), bid + 0.1, ask + 0.1)
    return PaperBroker(md, db_path=str(tmp_path / "p.db"),
                       clock=lambda: dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET))


def test_spread_shows_as_one_row_and_leftovers_as_singles(b):
    b.place_multileg([{"symbol": P(100), "side": "SELL", "ratio": 1},
                      {"symbol": P(95), "side": "BUY", "ratio": 1}], 2, "MARKET", strategy="Put Credit Spread")
    b.place_order(P(95), "BUY", 1)          # extra long put not part of the spread
    b.place_order("XYZ", "BUY", 10)
    p = pf.portfolio(b)
    groups = [r for r in p["options"] if r["kind"] == "group"]
    singles = [r for r in p["options"] if r["kind"] == "single"]
    assert len(groups) == 1 and groups[0]["title"] == "XYZ $100/$95 Put Credit Spread"
    assert groups[0]["holdings_sub"] == "2 spreads"
    assert groups[0]["cost"] == pytest.approx(-175 * 2)          # credit received
    assert len(singles) == 1 and singles[0]["title"] == "XYZ $95 Long Put"
    eq = p["equities"][0]
    assert eq["title"] == "XYZ" and eq["day_return"] == pytest.approx((100.0 - 98.0) * 10)


def test_close_group_in_one_click(b):
    b.place_multileg([{"symbol": C(100), "side": "BUY", "ratio": 1},
                      {"symbol": C(105), "side": "SELL", "ratio": 1}], 1, "MARKET", strategy="Call Debit Spread")
    row = pf.portfolio(b)["options"][0]
    o = pf.close_position(b, row["close"]["legs"], row["close"]["quantity"], row["close"]["strategy"])
    assert o["status"] == "FILLED" and o["strategy"] == "Close Call Debit Spread"
    assert b.positions() == [] and pf.portfolio(b)["options"] == []


def test_watchlist_persists_through_reset(b):
    pf.set_watchlist(b, ["qqq", "SPY", "QQQ", " aapl "])
    assert pf.get_watchlist(b) == ["QQQ", "SPY", "AAPL"]
    b.reset(10_000)
    assert pf.get_watchlist(b) == ["QQQ", "SPY", "AAPL"]


def test_equity_series_has_current_point(b):
    s = pf.equity_series(b, "1D")
    assert s["points"][-1]["equity"] == pytest.approx(25_000) and s["change"] == pytest.approx(0)
