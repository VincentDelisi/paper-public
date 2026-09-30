from fastapi.testclient import TestClient

from paper_public import server


def client(tmp_path):
    server.build(demo=True, db=str(tmp_path / "s.db"))
    server._cache.clear()
    return TestClient(server.app)


def test_trade_flow_through_api(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/account").json()["cash"] == 25000
    q = c.get("/api/quote?symbols=QQQ").json()["QQQ"]
    assert q["ask"] > q["bid"] > 0
    exp = c.get("/api/expirations?symbol=QQQ").json()[1]
    ch = c.get(f"/api/chain?symbol=QQQ&expiration={exp}&strikes=3").json()
    assert len(ch["rows"]) == 7
    atm = min(ch["rows"], key=lambda r: abs(r["strike"] - ch["spot"]))
    assert 0.3 < atm["call"]["delta"] < 0.7 and atm["call"]["iv"] > 0
    r = c.post("/api/orders", json={"symbol": atm["call"]["symbol"], "side": "BUY", "quantity": 1})
    assert r.status_code == 200 and r.json()["status"] == "FILLED"
    assert len(c.get("/api/positions").json()) == 1
    bad = c.post("/api/orders", json={"symbol": "QQQ", "side": "BUY", "quantity": 1000})
    assert bad.status_code == 422 and "buying power" in bad.json()["detail"]
    assert len(c.get("/api/bars?symbol=QQQ&period=DAY").json()) > 10
    assert c.post("/api/reset", json={"cash": 10000}).json()["cash"] == 10000


def test_cached_data_serves_last_good_quote_when_api_fails():
    import time
    from paper_public.market_data import CachedMarketData, MockMarketData

    class Flaky(MockMarketData):
        fail = False
        def quotes(self, symbols):
            if self.fail:
                raise TimeoutError("read timed out")
            return super().quotes(symbols)

    inner = Flaky(); inner.set("QQQ", 740.0, 740.1)
    c = CachedMarketData(inner, ttl=0.01)
    assert c.quotes(["QQQ"])["QQQ"].bid == 740.0
    inner.fail = True; time.sleep(0.05)
    q = c.quotes(["QQQ"])["QQQ"]
    assert q.bid == 740.0 and q.stale        # timeout -> last good quote, flagged stale


def test_chain_keeps_last_good_when_refresh_is_empty(tmp_path):
    c = client(tmp_path)
    exp = c.get("/api/expirations?symbol=QQQ").json()[1]
    first = c.get(f"/api/chain?symbol=QQQ&expiration={exp}&strikes=3").json()
    server._state["data"].inner.chain = lambda *a: []           # simulate API timeout
    import time
    key = ("chain", "QQQ", exp)
    server._cache[key] = (time.time() - 9, server._cache[key][1])      # expire the 8s TTL
    again = c.get(f"/api/chain?symbol=QQQ&expiration={exp}&strikes=3").json()
    assert len(again["rows"]) == len(first["rows"]) == 7


def test_backoff_stops_hammering_and_stale_quotes_never_fill(tmp_path):
    import datetime as dt
    import time
    from paper_public import OrderRejected, PaperBroker
    from paper_public.broker import ET
    from paper_public.market_data import CachedMarketData, MockMarketData

    class Counting(MockMarketData):
        calls, fail = 0, False
        def quotes(self, symbols):
            self.calls += 1
            if self.fail:
                raise TimeoutError("timed out")
            return super().quotes(symbols)

    inner = Counting(); inner.set("QQQ", 740.0, 740.1); inner.set("SPY", 600.0, 600.1)
    c = CachedMarketData(inner, ttl=0.01)
    c.quotes(["QQQ"]); c.quotes(["SPY"])
    assert inner.calls == 2
    inner.fail = True; time.sleep(0.05)
    c.quotes(["QQQ"])                                  # fails -> back off
    assert c.backing_off
    before = inner.calls
    for _ in range(20):
        c.quotes(["QQQ", "SPY"])                       # no API calls during back-off
    assert inner.calls == before
    time.sleep(0.05)
    b = PaperBroker(c, db_path=str(tmp_path / "b.db"),
                    clock=lambda: dt.datetime(2026, 9, 30, 10, 0, tzinfo=ET))
    import pytest
    with pytest.raises(OrderRejected, match="delayed"):
        b.place_order("QQQ", "BUY", 1)                 # stale quote -> no market fill


def test_agents_api(tmp_path):
    c = client(tmp_path)
    a = c.get("/api/agents").json()
    assert a["demo"] and len(a["agents"]) == 4 and not any(x["enabled"] for x in a["agents"])
    assert c.post("/api/agents/dip-buyer/start").json()["enabled"]
    server._state["agents"].step()
    d = c.get("/api/agents/dip-buyer").json()
    assert d["log"] and "portfolio" in d and d["equity"] == 25000
    assert c.get("/api/agents/feed").json()[0]["agent"] == "dip-buyer"
    assert not c.post("/api/agents/all/stop").json()["agents"][0]["enabled"]
    assert c.post("/api/agents/dip-buyer/reset").json()["equity"] == 25000
    assert c.get("/api/agents/nope").status_code == 404
    assert c.post("/api/agents/dip-buyer/explode").status_code == 400
    # agent books are separate from the user's account
    assert c.get("/api/account").json()["cash"] == 25000
