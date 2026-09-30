"""Local web app for the paper account.

    python -m paper_public.server            # live Public data (needs .env)
    python -m paper_public.server --demo     # synthetic data, no key needed

Opens http://127.0.0.1:8000 . Binds to localhost only: nothing on your
network or the internet can reach it.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agents import AgentRunner
from .broker import ET, OrderRejected, PaperBroker
from . import chainutil
from . import portfolio as pf
from .greeks import greeks
from .instruments import is_option, parse_option
from .market_data import CachedMarketData, MockMarketData, PublicMarketData

log = logging.getLogger("paper_public.server")
WEB = Path(__file__).parent / "web"
RISK_FREE = 0.04

_state: dict = {}
_lock = threading.RLock()          # SQLite + broker are not thread-safe on their own
_cache: dict[tuple, tuple[float, object]] = {}


def _cached(key: tuple, ttl: float, fn, keep_last_good: float = 600):
    """TTL cache. If a refresh comes back empty or raises (API slow/timeout),
    keep serving the last good value for up to `keep_last_good` seconds so
    the screen doesn't flicker to blank."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:  # noqa: BLE001
        log.warning("%s failed: %s", key[0], e)
        val = None
    if not val and hit and now - hit[0] < keep_last_good:
        return hit[1]
    _cache[key] = (now, val)
    return val


def broker() -> PaperBroker:
    return _state["broker"]


def data():
    return _state["data"]


def _years_to_expiry(exp: dt.date) -> float:
    close = dt.datetime.combine(exp, dt.time(16, 0), tzinfo=ET)
    secs = (close - dt.datetime.now(ET)).total_seconds()
    return max(secs, 60.0) / (365.0 * 86400.0)


async def _process_loop():
    while True:
        try:
            with _lock:
                broker().process()
        except Exception as e:  # noqa: BLE001
            log.warning("process loop: %s", e)
        await asyncio.sleep(5)


async def _agent_loop():
    while True:
        r = _state.get("agents")
        if r is not None:
            try:
                await asyncio.to_thread(r.step)
            except Exception as e:  # noqa: BLE001
                log.warning("agent loop: %s", e)
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_process_loop()), asyncio.create_task(_agent_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Shadow Book", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


# ------------------------------------------------------------------ data
@app.get("/api/meta")
def meta():
    st = data().status() if hasattr(data(), "status") else {}
    return {"demo": _state.get("demo", False), "market_open": broker().market_open(),
            "now": dt.datetime.now(ET).isoformat(), "data": st}


@app.get("/api/quote")
def quote(symbols: str):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    qs = data().quotes(syms)
    return {s: {"bid": q.bid, "ask": q.ask, "last": q.last, "mid": round(q.mid, 4),
                "previous_close": q.previous_close, "stale": q.stale,
                "timestamp": q.timestamp.isoformat() if q.timestamp else None}
            for s, q in qs.items()}


@app.get("/api/bars")
def bars(symbol: str, period: str = "DAY"):
    period = period.upper()
    if period not in ("DAY", "WEEK", "MONTH", "YEAR"):
        raise HTTPException(400, "period must be DAY, WEEK, MONTH or YEAR")
    return _cached(("bars", symbol.upper(), period), 60, lambda: data().bars(symbol.upper(), period)) or []


@app.get("/api/expirations")
def expirations(symbol: str):
    return _cached(("exp", symbol.upper()), 300, lambda: data().expirations(symbol.upper())) or []


@app.get("/api/chain")
def chain(symbol: str, expiration: str, strikes: int = Query(12, ge=2, le=60)):
    symbol = symbol.upper()
    rows = _cached(("chain", symbol, expiration), 15, lambda: data().chain(symbol, expiration)) or []
    q = data().quotes([symbol]).get(symbol)
    spot = (q.last or q.mid) if q else None
    if not rows:
        return {"spot": spot, "rows": []}
    exp = dt.date.fromisoformat(expiration)
    ordered = chainutil.enrich(rows, spot, expiration)
    if spot:
        atm = min(range(len(ordered)), key=lambda i: abs(ordered[i]["strike"] - spot))
        ordered = ordered[max(0, atm - strikes): atm + strikes + 1]
    return {"spot": spot, "expiration": expiration,
            "dte": (exp - dt.datetime.now(ET).date()).days, "rows": ordered}


# ------------------------------------------------------------------ account
@app.get("/api/account")
def account():
    with _lock:
        return broker().account()


@app.get("/api/positions")
def positions():
    with _lock:
        out = broker().positions()
    for p in out:
        opt = parse_option(p["symbol"])
        p["description"] = (f"{opt.underlying} {opt.expiration:%b %d} ${opt.strike:g} "
                            f"{'Call' if opt.right == 'C' else 'Put'}") if opt else p["symbol"]
    return out


@app.get("/api/orders")
def orders(open_only: bool = False):
    with _lock:
        return broker().orders(open_only=open_only, limit=100)


@app.get("/api/fills")
def fills():
    with _lock:
        return broker().fills(limit=100)


class OrderIn(BaseModel):
    symbol: str
    side: str
    quantity: float
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None


@app.post("/api/orders")
def place(o: OrderIn):
    with _lock:
        try:
            return broker().place_order(o.symbol, o.side, o.quantity, o.order_type,
                                        o.limit_price, o.time_in_force, stop_price=o.stop_price,
                                        take_profit=o.take_profit, stop_loss=o.stop_loss)
        except OrderRejected as e:
            raise HTTPException(422, str(e))


class LegIn(BaseModel):
    symbol: str
    side: str
    ratio: float = 1


class MultiIn(BaseModel):
    legs: list[LegIn]
    quantity: int
    order_type: str = "LIMIT"
    net_price: Optional[float] = None
    time_in_force: str = "DAY"
    strategy: Optional[str] = None


@app.post("/api/multileg")
def place_multi(o: MultiIn):
    with _lock:
        try:
            return broker().place_multileg([l.model_dump() for l in o.legs], o.quantity, o.order_type,
                                           o.net_price, o.time_in_force, o.strategy)
        except OrderRejected as e:
            raise HTTPException(422, str(e))


@app.get("/api/portfolio")
def portfolio_view():
    with _lock:
        return pf.portfolio(broker())


@app.get("/api/equity")
def equity(range: str = "1D"):  # noqa: A002
    with _lock:
        return pf.equity_series(broker(), range)


@app.get("/api/market")
def market_strip():
    syms = [s for _, s in pf.MARKET_STRIP]
    qs = data().quotes(syms)
    out = []
    for label, s in pf.MARKET_STRIP:
        q = qs.get(s)
        last = (q.last or q.mid) if q else None
        pc = q.previous_close if q else None
        out.append({"label": label, "symbol": s, "last": last,
                    "change_pct": round((last - pc) / pc * 100, 2) if (last and pc) else None})
    return out


@app.get("/api/watchlist")
def watchlist():
    with _lock:
        syms = pf.get_watchlist(broker())
    qs = data().quotes(syms) if syms else {}
    out = []
    for s in syms:
        q = qs.get(s)
        last = (q.last or q.mid) if q else None
        pc = q.previous_close if q else None
        out.append({"symbol": s, "last": last, "change": round(last - pc, 2) if (last and pc) else None,
                    "change_pct": round((last - pc) / pc * 100, 2) if (last and pc) else None})
    return out


class WatchIn(BaseModel):
    symbols: list[str]


@app.put("/api/watchlist")
def watchlist_set(w: WatchIn):
    with _lock:
        return pf.set_watchlist(broker(), w.symbols)


class CloseIn(BaseModel):
    legs: list[LegIn]
    quantity: float
    strategy: Optional[str] = None
    order_type: str = "MARKET"
    net_price: Optional[float] = None


@app.post("/api/close")
def close(c: CloseIn):
    with _lock:
        try:
            return pf.close_position(broker(), [l.model_dump() for l in c.legs], c.quantity,
                                     c.strategy, c.order_type.upper(), c.net_price)
        except OrderRejected as e:
            raise HTTPException(422, str(e))


@app.get("/api/analytics")
def analytics():
    with _lock:
        return broker().analytics()


@app.delete("/api/orders/{order_id}")
def cancel(order_id: str):
    with _lock:
        try:
            return broker().cancel_order(order_id)
        except KeyError:
            raise HTTPException(404, "order not found")


class ResetIn(BaseModel):
    cash: float = 25_000.0


@app.post("/api/reset")
def reset(r: ResetIn):
    with _lock:
        broker().reset(r.cash)
        return broker().account()


# ------------------------------------------------------------------ agents
def runner() -> AgentRunner:
    return _state["agents"]


@app.get("/api/agents")
def agents_list():
    return {"demo": _state.get("demo", False), "agents": runner().summaries()}


@app.get("/api/agents/feed")
def agents_feed(limit: int = Query(60, ge=1, le=300)):
    return runner().feed(limit)


@app.get("/api/agents/{agent_id}")
def agent_detail(agent_id: str):
    try:
        return runner().detail(agent_id)
    except KeyError:
        raise HTTPException(404, "no such agent")


@app.post("/api/agents/{agent_id}/{action}")
def agent_action(agent_id: str, action: str):
    r = runner()
    if agent_id == "all":
        if action == "start":
            r.start_all()
        elif action == "stop":
            r.stop_all()
        else:
            raise HTTPException(400, "all supports start or stop")
        return {"agents": r.summaries()}
    fn = {"start": r.start, "stop": r.stop, "reset": r.reset, "flatten": r.flatten}.get(action)
    if fn is None:
        raise HTTPException(400, "action must be start, stop, reset or flatten")
    try:
        out = fn(agent_id)
    except KeyError:
        raise HTTPException(404, "no such agent")
    return {**r.summary(agent_id), "result": out}


# ------------------------------------------------------------------ main
def build(demo: bool, db: str) -> None:
    if demo:
        md = CachedMarketData(MockMarketData.demo())
        _state.update(data=md, demo=True,
                      broker=PaperBroker(md, db_path=db, enforce_market_hours=False))
    else:
        md = CachedMarketData(PublicMarketData())
        _state.update(data=md, demo=False, broker=PaperBroker(md, db_path=db))
    folder = Path(db).resolve().parent / ("demo_agent_books" if demo else "agent_books")
    _state["agents"] = AgentRunner(_state["data"], folder, demo=demo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="synthetic data, no API key")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default=None, help="account file (default paper_account.db, demo_account.db in demo)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build(a.demo, a.db or ("demo_account.db" if a.demo else "paper_account.db"))
    url = f"http://127.0.0.1:{a.port}"
    print(f"\n  Shadow Book running at {url}  ({'DEMO data' if a.demo else 'live Public data'})"
          f"\n  Ctrl+C to stop\n")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
