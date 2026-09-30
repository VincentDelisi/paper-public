import datetime as dt

import pytest

from paper_public import MockMarketData
from paper_public.agents import (AgentRunner, CoveredCallWriter, DipBuyer, MomentumCallScalper,
                                 PutCreditSpreadSeller)
from paper_public.broker import ET


class Clock:
    def __init__(self):
        d = dt.date.today()
        self.t = dt.datetime(d.year, d.month, d.day, 11, 0, tzinfo=ET)
    def __call__(self): return self.t
    def advance(self, s): self.t += dt.timedelta(seconds=s)


def make(tmp_path, agent):
    md = MockMarketData.demo()
    clock = Clock()
    r = AgentRunner(md, tmp_path / "agents", demo=True, agents=[agent], clock=clock)
    r.start(agent.id)
    return r, md, clock


def run(r, clock, secs=5, n=1):
    for _ in range(n):
        clock.advance(secs)
        r.step(now=clock().timestamp())


def logs(r, aid):
    return [x["message"] for x in r.detail(aid)["log"]][::-1]


def test_dip_buyer_buys_the_dip_and_takes_profit(tmp_path):
    r, md, clock = make(tmp_path, DipBuyer())
    b = r.slots["dip-buyer"].broker
    md.set("SPY", 599.99, 600.01)
    run(r, clock)
    assert b._pos_map() == {}
    md.set("SPY", 597.49, 597.51)                  # -0.42% from the high
    run(r, clock)
    assert b._pos_map() == {"SPY": 20}
    md.set("SPY", 599.40, 599.42)                  # +0.32% from entry
    run(r, clock)
    assert b._pos_map() == {}
    text = " ".join(logs(r, "dip-buyer"))
    assert "below today's high" in text and "Taking profit" in text and "Realized +$" in text
    s = r.summary("dip-buyer")
    assert s["closed_trades"] == 1 and s["wins"] == 1 and s["total_pnl"] > 0


def test_momentum_scalper_enters_with_bracket_and_hits_take_profit(tmp_path):
    r, md, clock = make(tmp_path, MomentumCallScalper())
    b = r.slots["momentum-calls"].broker
    md.set("QQQ", 499.99, 500.01)
    run(r, clock, 5, 50)                          # ~4 minutes of flat history
    assert b._pos_map() == {}
    md.set("QQQ", 500.99, 501.01)                 # breakout
    run(r, clock)
    pos = b._pos_map()
    assert len(pos) == 1 and list(pos.values())[0] == 1
    call = next(iter(pos))
    exits = b.orders(open_only=True)
    assert {o["order_type"] for o in exits} == {"LIMIT", "STOP"}
    md.set("QQQ", 520.0, 520.02)                  # call rallies far past +25%
    run(r, clock)
    assert b._pos_map() == {}
    text = " ".join(logs(r, "momentum-calls"))
    assert "Breakout" in text and "bracket armed" in text and "Take-profit hit" in text
    assert call not in b._pos_map()


def test_put_spread_walks_price_then_fills_and_takes_profit(tmp_path):
    r, md, clock = make(tmp_path, PutCreditSpreadSeller())
    b = r.slots["put-spreads"].broker
    md.set("SPY", 599.99, 600.01)
    run(r, clock, 10)
    w = r._ctx(r.slots["put-spreads"]).state.get("working")
    assert w and b.get_order(w["id"])["status"] == "OPEN"     # resting at the mid
    run(r, clock, 10, 12)                                       # walks 3 steps to the natural
    st = r._ctx(r.slots["put-spreads"]).state
    assert "spread" in st and "working" not in st
    pos = b._pos_map()
    assert pos[st["spread"]["short"]] == -1 and pos[st["spread"]["long"]] == 1
    assert b.account()["collateral"] == pytest.approx(500, abs=1)
    md.set("SPY", 640.0, 640.02)                               # rally: spread decays
    run(r, clock, 10)
    assert b._pos_map() == {}
    text = " ".join(logs(r, "put-spreads"))
    assert "Walking the price" in text and "Spread is on" in text and "Taking profit" in text


def test_covered_call_opens_as_one_order_and_flatten_closes(tmp_path):
    r, md, clock = make(tmp_path, CoveredCallWriter())
    b = r.slots["covered-calls"].broker
    md.set("SOFI", 16.19, 16.21)
    run(r, clock, 15)
    pos = b._pos_map()
    assert pos.get("SOFI") == 100
    call = [s for s in pos if s != "SOFI"][0]
    assert pos[call] == -1
    assert b.account()["collateral"] == 0                     # shares cover the call
    run(r, clock, 15)
    assert "Holding 100 SOFI" in " ".join(logs(r, "covered-calls"))
    out = r.flatten("covered-calls")
    assert out["closed"] == 2 and b._pos_map() == {}
    assert not r.slots["covered-calls"].enabled


def test_runner_pause_reset_and_feed(tmp_path):
    md = MockMarketData.demo()
    clock = Clock()
    r = AgentRunner(md, tmp_path / "a", demo=True, clock=clock)
    assert len(r.slots) == 4 and not any(s.enabled for s in r.slots.values())
    md.set("SPY", 599.99, 600.01)
    run(r, clock)                                  # paused: nothing happens
    assert all(s.broker._pos_map() == {} for s in r.slots.values())
    r.start_all()
    run(r, clock, 15)
    feed = r.feed()
    assert {f["agent"] for f in feed} >= {"dip-buyer", "put-spreads"}
    r.stop("dip-buyer")
    assert not r.slots["dip-buyer"].enabled
    r.reset("put-spreads")
    s = r.summary("put-spreads")
    assert s["equity"] == 25_000 and s["open_orders"] == 0 and not s["enabled"]
