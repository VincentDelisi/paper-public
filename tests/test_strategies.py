"""Every strategy card: collateral, fills, P&L and settlement."""
import datetime as dt

import pytest

from paper_public import MockMarketData, OrderRejected, PaperBroker
from paper_public.broker import ET
from paper_public.instruments import option_symbol

EXP = dt.date(2026, 10, 16)
C = lambda k: option_symbol("XYZ", EXP, "C", k)   # noqa: E731
P = lambda k: option_symbol("XYZ", EXP, "P", k)   # noqa: E731


class Clock:
    def __init__(self): self.t = dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET)
    def __call__(self): return self.t


@pytest.fixture
def env(tmp_path):
    md = MockMarketData()
    md.set("XYZ", 99.99, 100.01, last=100.0)
    for k, cb, ca, pb, pa in [(90, 10.4, 10.6, 0.40, 0.45), (95, 6.1, 6.2, 1.10, 1.15),
                              (100, 3.0, 3.1, 2.9, 3.0), (105, 1.2, 1.25, 6.0, 6.2), (110, 0.4, 0.45, 10.3, 10.6)]:
        md.set(C(k), cb, ca); md.set(P(k), pb, pa)
    clock = Clock()
    b = PaperBroker(md, db_path=str(tmp_path / "s.db"), starting_cash=25_000, clock=clock)
    return b, md, clock


def bp(b): return b.account()["buying_power"]


def test_long_call_and_long_put_cost_premium_only(env):
    b, _, _ = env
    b.place_order(C(100), "BUY", 1)
    assert bp(b) == pytest.approx(25_000 - 310)
    b.place_order(P(100), "BUY", 1)
    assert bp(b) == pytest.approx(25_000 - 310 - 300)


def test_cash_secured_put_reserves_strike(env):
    b, _, _ = env
    b.place_order(P(95), "SELL", 1)                      # sell to open
    assert b.positions()[0]["quantity"] == -1
    assert b.cash == pytest.approx(25_000 + 110)          # premium received at the bid
    assert b.account()["collateral"] == pytest.approx(9_500)
    assert bp(b) == pytest.approx(25_000 + 110 - 9_500)


def test_covered_call_needs_no_extra_collateral(env):
    b, _, _ = env
    o = b.place_multileg([{"symbol": "XYZ", "side": "BUY", "ratio": 100},
                          {"symbol": C(105), "side": "SELL", "ratio": 1}], 1, "MARKET",
                         strategy="Covered Call")
    assert o["status"] == "FILLED"
    assert o["net_fill"] == pytest.approx(100.01 - 1.2)   # stock at ask, call at bid
    assert b.account()["collateral"] == 0
    assert bp(b) == pytest.approx(25_000 - 10_001 + 120)


def test_naked_call_alone_is_stress_tested(env):
    b, _, _ = env
    b.place_order(C(105), "SELL", 1)
    # worst case at 1.5x spot = 150: (150-105)*100 = 4,500
    assert b.account()["collateral"] == pytest.approx(4_500)


def test_put_credit_spread_collateral_is_width(env):
    b, _, _ = env
    o = b.place_multileg([{"symbol": P(100), "side": "SELL", "ratio": 1},
                          {"symbol": P(95), "side": "BUY", "ratio": 1}], 2, "LIMIT", net_price=-1.50,
                         strategy="Put Credit Spread")
    assert o["status"] == "FILLED"                          # natural: -2.90 + 1.15 = -1.75 <= -1.50
    assert o["net_fill"] == pytest.approx(-1.75)
    assert b.account()["collateral"] == pytest.approx(5 * 100 * 2)
    assert b.cash == pytest.approx(25_000 + 350)


def test_call_debit_spread_limit_rests_then_fills(env):
    b, md, _ = env
    o = b.place_multileg([{"symbol": C(100), "side": "BUY", "ratio": 1},
                          {"symbol": C(105), "side": "SELL", "ratio": 1}], 1, "LIMIT", net_price=1.70)
    assert o["status"] == "OPEN"                            # natural 3.10-1.20 = 1.90 > 1.70
    assert bp(b) == pytest.approx(25_000 - 170, abs=1)      # reserved while resting
    md.set(C(100), 2.8, 2.85)
    b.process()
    f = b.get_order(o["id"])
    assert f["status"] == "FILLED" and f["net_fill"] == pytest.approx(1.65)
    assert b.account()["collateral"] == 0


def test_straddle_strangle_and_synthetics(env):
    b, _, _ = env
    assert b.place_multileg([{"symbol": C(100), "side": "BUY", "ratio": 1},
                             {"symbol": P(100), "side": "BUY", "ratio": 1}], 1, "MARKET")["status"] == "FILLED"
    assert b.place_multileg([{"symbol": C(110), "side": "BUY", "ratio": 1},
                             {"symbol": P(90), "side": "BUY", "ratio": 1}], 1, "MARKET")["status"] == "FILLED"
    b.reset(25_000)
    # synthetic long = long call + short put -> put is cash-secured
    b.place_multileg([{"symbol": C(100), "side": "BUY", "ratio": 1},
                      {"symbol": P(100), "side": "SELL", "ratio": 1}], 1, "MARKET")
    assert b.account()["collateral"] == pytest.approx(10_000)
    b.reset(25_000)
    # synthetic short = long put + short call -> call stressed to 1.5x
    b.place_multileg([{"symbol": P(100), "side": "BUY", "ratio": 1},
                      {"symbol": C(100), "side": "SELL", "ratio": 1}], 1, "MARKET")
    assert b.account()["collateral"] == pytest.approx(5_000)


def test_covered_put_and_protective_put(env):
    b, _, _ = env
    b.place_multileg([{"symbol": "XYZ", "side": "SELL", "ratio": 100},
                      {"symbol": P(95), "side": "SELL", "ratio": 1}], 1, "MARKET", strategy="Covered Put")
    pos = {p["symbol"]: p["quantity"] for p in b.positions()}
    assert pos["XYZ"] == -100 and pos[P(95)] == -1
    # short 100 @ 99.99 + short 95 put: worst case at 150 -> 100*150 = 15,000
    assert b.account()["collateral"] == pytest.approx(15_000)
    b.reset(25_000)
    b.place_multileg([{"symbol": "XYZ", "side": "BUY", "ratio": 100},
                      {"symbol": P(95), "side": "BUY", "ratio": 1}], 1, "MARKET", strategy="Protective Put")
    assert b.account()["collateral"] == 0


def test_closing_short_and_settlement(env):
    b, md, clock = env
    b.place_order(P(100), "SELL", 1)                        # +290
    md.set(P(100), 1.0, 1.1)
    b.place_order(P(100), "BUY", 1)                         # buy to close at 1.10
    assert b.fills()[0]["realized_pnl"] == pytest.approx(180)
    assert b.fills()[0]["effect"] == "CLOSE"
    # short call assigned at expiration: pay intrinsic
    b.place_order(C(105), "SELL", 1)                        # +120
    clock.t = dt.datetime(2026, 10, 16, 16, 5, tzinfo=ET)
    md.set("XYZ", 107.99, 108.01, last=108.0)
    b.process()
    f = b.fills()[0]
    assert f["effect"] == "ASSIGNED" and f["realized_pnl"] == pytest.approx(120 - 300)


def test_cannot_flip_or_exceed_buying_power(env):
    b, _, _ = env
    b.place_order(C(100), "BUY", 1)
    with pytest.raises(OrderRejected, match="exceeds position"):
        b.place_order(C(100), "SELL", 2)
    with pytest.raises(OrderRejected, match="buying power"):
        b.place_order(P(110), "SELL", 3)                    # 3 x 11,000 collateral > equity


def test_stop_loss_triggers_and_bracket_is_oco(env):
    b, md, _ = env
    o = b.place_order(C(100), "BUY", 2, take_profit=4.0, stop_loss=2.0)
    assert o["status"] == "FILLED"
    kids = [x for x in b.orders(open_only=True) if x.get("parent_id") == o["id"]]
    assert {k["order_type"] for k in kids} == {"LIMIT", "STOP"}
    md.set(C(100), 1.95, 2.05)                              # bid below stop -> stop triggers
    b.process()
    stop = next(k for k in kids if k["order_type"] == "STOP")
    tp = next(k for k in kids if k["order_type"] == "LIMIT")
    assert b.get_order(stop["id"])["status"] == "FILLED"
    assert b.get_order(tp["id"])["status"] == "CANCELLED"
    assert b.positions() == []
    a = b.analytics()
    assert a["closed_trades"] == 1 and a["win_rate"] == 0.0


def test_stop_limit_waits_for_limit_after_trigger(env):
    b, md, _ = env
    b.place_order(C(100), "BUY", 1)
    o = b.place_order(C(100), "SELL", 1, "STOP_LIMIT", limit_price=2.5, stop_price=2.6)
    md.set(C(100), 2.4, 2.45)                               # triggered, but bid < limit
    b.process()
    assert b.get_order(o["id"])["trigger_state"] == "TRIGGERED"
    assert b.get_order(o["id"])["status"] == "OPEN"
    md.set(C(100), 2.55, 2.6)
    b.process()
    assert b.get_order(o["id"])["status"] == "FILLED"


def test_old_account_file_is_upgraded(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE orders (id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, symbol TEXT, side TEXT,
      quantity REAL, order_type TEXT, limit_price REAL, time_in_force TEXT, status TEXT,
      filled_quantity REAL DEFAULT 0, avg_fill_price REAL, reason TEXT);
      CREATE TABLE fills (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts TEXT, trade_date TEXT,
      symbol TEXT, side TEXT, quantity REAL, price REAL, fee REAL, realized_pnl REAL);
      INSERT INTO meta VALUES('cash','24000'),('starting_cash','25000');""")
    db.commit(); db.close()
    md = MockMarketData(); md.set("SPY", 600, 600.1)
    b = PaperBroker(md, db_path=path, clock=Clock())
    assert b.cash == 24_000
    assert b.place_order("SPY", "BUY", 1)["status"] == "FILLED"


def test_market_credit_spread_is_labeled_credit(env):
    b, _, _ = env
    o = b.place_multileg([{"symbol": P(100), "side": "SELL", "ratio": 1},
                          {"symbol": P(95), "side": "BUY", "ratio": 1}], 1, "MARKET")
    assert o["side"] == "CREDIT" and o["net_fill"] < 0
