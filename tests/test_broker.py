import datetime as dt

import pytest

from paper_public import MockMarketData, OrderRejected, PaperBroker
from paper_public.broker import ET
from paper_public.instruments import option_symbol, parse_option


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def set(self, *a): self.t = dt.datetime(*a, tzinfo=ET)


@pytest.fixture
def env(tmp_path):
    md = MockMarketData()
    clock = Clock(dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET))  # Wednesday
    b = PaperBroker(md, db_path=str(tmp_path / "t.db"), starting_cash=25_000, clock=clock)
    return b, md, clock


def test_market_buy_fills_at_ask_and_sell_at_bid(env):
    b, md, _ = env
    md.set("QQQ", 739.90, 740.10)
    o = b.place_order("QQQ", "BUY", 10)
    assert o["status"] == "FILLED" and o["avg_fill_price"] == 740.10
    assert b.cash == pytest.approx(25_000 - 7401.0)
    md.set("QQQ", 741.00, 741.20)
    s = b.place_order("QQQ", "SELL", 10)
    assert s["avg_fill_price"] == 741.00
    assert b.fills()[0]["realized_pnl"] == pytest.approx(9.0)   # (741.00-740.10)*10
    assert b.cash == pytest.approx(25_009.0)
    assert b.positions() == []


def test_limit_rests_until_quote_reaches_it(env):
    b, md, _ = env
    md.set("SPY", 600.00, 600.20)
    o = b.place_order("SPY", "BUY", 5, "LIMIT", 599.50)
    assert o["status"] == "OPEN"
    assert b.account()["buying_power"] == pytest.approx(25_000 - 599.50 * 5)  # cash reserved
    md.set("SPY", 599.20, 599.40)
    assert b.process()["filled"] == 1
    f = b.get_order(o["id"])
    assert f["status"] == "FILLED" and f["avg_fill_price"] == 599.40  # filled at ask, better than limit


def test_option_multiplier_and_expiration_settles_itm(env):
    b, md, clock = env
    sym = option_symbol("QQQ", dt.date(2026, 10, 2), "C", 740)
    assert sym == "QQQ261002C00740000" and parse_option(sym).strike == 740
    md.set(sym, 3.30, 3.40)
    b.place_order(sym, "BUY", 2)
    assert b.cash == pytest.approx(25_000 - 3.40 * 2 * 100)
    clock.set(2026, 10, 2, 16, 5)          # after expiration close
    md.set("QQQ", 745.00, 745.10, last=745.00)
    assert b.process()["settled"] == 1
    assert b.positions() == []
    # intrinsic 5.00 * 2 * 100 = 1000 credited; realized = (5.00-3.40)*200 = 320
    assert b.cash == pytest.approx(25_000 - 680 + 1000)
    assert b.fills()[0]["realized_pnl"] == pytest.approx(320)


def test_otm_option_expires_worthless(env):
    b, md, clock = env
    sym = option_symbol("QQQ", dt.date(2026, 9, 30), "P", 700)
    md.set(sym, 0.05, 0.07)
    b.place_order(sym, "BUY", 1)
    clock.set(2026, 9, 30, 16, 1)
    md.set("QQQ", 739, 739.1, last=739)
    b.process()
    assert b.fills()[0]["realized_pnl"] == pytest.approx(-7.0)


def test_rejections(env):
    b, md, clock = env
    md.set("QQQ", 739.90, 740.10)
    with pytest.raises(OrderRejected, match="buying power"):
        b.place_order("QQQ", "BUY", 100)
    with pytest.raises(OrderRejected, match="whole number"):
        b.place_order("QQQ261002C00740000", "BUY", 1.5)
    clock.set(2026, 9, 30, 17, 0)
    with pytest.raises(OrderRejected, match="market is closed"):
        b.place_order("QQQ", "BUY", 1)
    assert b.orders()[0]["status"] == "REJECTED"   # rejections are logged


def test_cannot_oversell_with_pending_sells(env):
    b, md, _ = env
    md.set("QQQ", 739.90, 740.10)
    b.place_order("QQQ", "BUY", 5)
    b.place_order("QQQ", "SELL", 3, "LIMIT", 800)
    with pytest.raises(OrderRejected, match="exceeds position"):
        b.place_order("QQQ", "SELL", 3)


def test_day_order_expires_and_after_hours_order_waits_for_next_session(env):
    b, md, clock = env
    md.set("QQQ", 739.90, 740.10)
    o1 = b.place_order("QQQ", "BUY", 1, "LIMIT", 700)
    clock.set(2026, 9, 30, 16, 30)
    o2 = b.place_order("QQQ", "BUY", 1, "LIMIT", 700)    # placed after close
    b.process()
    assert b.get_order(o1["id"])["status"] == "EXPIRED"
    assert b.get_order(o2["id"])["status"] == "OPEN"      # good for Thursday's session
    clock.set(2026, 10, 1, 16, 0)
    b.process()
    assert b.get_order(o2["id"])["status"] == "EXPIRED"


def test_pdt_off_by_default_and_optional(env, tmp_path):
    b, md, clock = env
    md.set("SPY", 600.0, 600.1)
    for _ in range(4):                       # no PDT limit by default
        b.place_order("SPY", "BUY", 1)
        b.place_order("SPY", "SELL", 1)
    b = PaperBroker(md, db_path=str(tmp_path / "pdt.db"), clock=clock, enforce_pdt=True)
    md.set("SPY", 600.0, 600.1)
    for _ in range(3):
        b.place_order("SPY", "BUY", 1)
        b.place_order("SPY", "SELL", 1)
    assert b.day_trades_count() == 3
    with pytest.raises(OrderRejected, match="PDT"):
        b.place_order("SPY", "BUY", 1)


def test_daily_loss_limit(tmp_path):
    md = MockMarketData()
    clock = Clock(dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET))
    b = PaperBroker(md, db_path=str(tmp_path / "l.db"), daily_loss_limit=100, clock=clock, enforce_pdt=False)
    md.set("SPY", 600.0, 600.1)
    b.account()                                   # start-of-day snapshot
    b.place_order("SPY", "BUY", 10)
    md.set("SPY", 585.0, 585.1)
    b.place_order("SPY", "SELL", 10)              # closing always allowed
    with pytest.raises(OrderRejected, match="daily loss limit"):
        b.place_order("SPY", "BUY", 1)


def test_state_persists_across_restarts(tmp_path):
    md = MockMarketData(); md.set("QQQ", 739.9, 740.1)
    clock = Clock(dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET))
    path = str(tmp_path / "p.db")
    PaperBroker(md, db_path=path, clock=clock).place_order("QQQ", "BUY", 2)
    b2 = PaperBroker(md, db_path=path, clock=clock)
    assert b2.positions()[0]["quantity"] == 2
    assert b2.cash == pytest.approx(25_000 - 1480.2)
