"""Portfolio view: groups spread legs back into strategies, adds day change,
and keeps the watchlist. Pure read-side logic on top of PaperBroker."""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from .broker import ET, PaperBroker, underlying_of
from .instruments import multiplier, parse_option

DEFAULT_WATCHLIST = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMZN", "META"]
MARKET_STRIP = [("S&P 500", "SPY"), ("Nasdaq 100", "QQQ"), ("Dow 30", "DIA"),
                ("Russell 2000", "IWM"), ("20Y Treasury", "TLT")]


# ------------------------------------------------------------------ watchlist
def get_watchlist(b: PaperBroker) -> list[str]:
    raw = b.ledger.get("watchlist")
    if raw is None:
        return list(DEFAULT_WATCHLIST)
    try:
        return [s for s in json.loads(raw) if isinstance(s, str)]
    except (ValueError, TypeError):
        return list(DEFAULT_WATCHLIST)


def set_watchlist(b: PaperBroker, symbols: list[str]) -> list[str]:
    clean: list[str] = []
    for s in symbols:
        s = s.strip().upper()
        if s and s not in clean and len(s) <= 22:
            clean.append(s)
    b.ledger.set("watchlist", json.dumps(clean[:50]))
    b.ledger.commit()
    return clean


# ------------------------------------------------------------------ helpers
def _mid(q) -> Optional[float]:
    return q.mid if q is not None and q.valid else None


def _fmt_strike(k: float) -> str:
    return f"${k:g}"


def _describe_single(sym: str, qty: float) -> tuple[str, str]:
    opt = parse_option(sym)
    if not opt:
        return sym, ""
    side = "Long" if qty > 0 else "Short"
    kind = "Call" if opt.right == "C" else "Put"
    return (f"{opt.underlying} {_fmt_strike(opt.strike)} {side} {kind}",
            f"Expires {opt.expiration:%b} {opt.expiration.day}")


def _describe_group(strategy: str, legs: list[dict]) -> tuple[str, str]:
    opts = [parse_option(l["symbol"]) for l in legs]
    und = next((o.underlying for o in opts if o), underlying_of(legs[0]["symbol"]))
    strikes = sorted({o.strike for o in opts if o}, reverse=True)
    exps = sorted({o.expiration for o in opts if o})
    title = f"{und} {'/'.join(_fmt_strike(k) for k in strikes)} {strategy}".replace("  ", " ")
    sub = "Expires " + ", ".join(f"{e:%b} {e.day}" for e in exps) if exps else "Shares + options"
    return title, sub


def _pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return round(num / abs(den) * 100, 2)


# ------------------------------------------------------------------ portfolio
def portfolio(b: PaperBroker) -> dict:
    pos_rows = b.ledger.rows("SELECT * FROM positions WHERE quantity != 0")
    remaining = {p["symbol"]: float(p["quantity"]) for p in pos_rows}
    avg = {p["symbol"]: float(p["avg_cost"]) for p in pos_rows}
    quotes = b._safe_quotes(remaining.keys()) if remaining else {}

    groups = []
    ml = b.ledger.rows("SELECT * FROM orders WHERE order_class='MULTILEG' AND status='FILLED' "
                       "AND COALESCE(strategy,'') NOT LIKE 'Close %' "
                       "ORDER BY updated_at DESC, rowid DESC")
    for o in ml:
        legs = json.loads(o["legs"])
        k = int(o["quantity"])
        for l in legs:
            want = 1 if l["side"] == "BUY" else -1
            have = remaining.get(l["symbol"], 0.0)
            if have * want <= 0:
                k = 0
                break
            k = min(k, int(abs(have) // l["ratio"]))
        if k < 1:
            continue
        for l in legs:
            sign = 1 if l["side"] == "BUY" else -1
            remaining[l["symbol"]] -= sign * l["ratio"] * k
        groups.append((o, legs, k))

    rows_opt, rows_eq = [], []
    for o, legs, k in groups:
        mids = {l["symbol"]: _mid(quotes.get(l["symbol"])) for l in legs}
        prevs = {l["symbol"]: getattr(quotes.get(l["symbol"]), "previous_close", None) for l in legs}
        per = lambda l: (1 if l["side"] == "BUY" else -1) * l["ratio"] * multiplier(l["symbol"]) / 100  # noqa: E731
        net_mark = sum(per(l) * mids[l["symbol"]] for l in legs) if all(v is not None for v in mids.values()) else None
        net_prev = sum(per(l) * prevs[l["symbol"]] for l in legs) if all(prevs.values()) else None
        value = net_mark * 100 * k if net_mark is not None else None
        cost = (o["net_fill"] or 0) * 100 * k
        day = (net_mark - net_prev) * 100 * k if (net_mark is not None and net_prev is not None) else None
        strat = o["strategy"] or "Spread"
        title, sub = _describe_group(strat, legs)
        has_stock = any(not parse_option(l["symbol"]) for l in legs)
        row = {
            "kind": "group", "title": title, "subtitle": sub, "strategy": strat,
            "underlying": underlying_of(legs[0]["symbol"]),
            "price": round(net_mark, 2) if net_mark is not None else None,
            "price_change": round(net_mark - net_prev, 2) if day is not None else None,
            "price_change_pct": _pct(net_mark - net_prev, net_prev) if day is not None else None,
            "holdings": round(value, 2) if value is not None else None,
            "holdings_sub": f"{k} {'position' if has_stock else 'spread'}{'s' if k != 1 else ''}",
            "cost": round(cost, 2), "cost_sub": f"${abs(o['net_fill'] or 0):.2f}/share {'debit' if (o['net_fill'] or 0) >= 0 else 'credit'}",
            "day_return": round(day, 2) if day is not None else None,
            "day_return_pct": _pct(day, value - day) if (day is not None and value is not None) else None,
            "unrealized": round(value - cost, 2) if value is not None else None,
            "unrealized_pct": _pct(value - cost, cost) if value is not None else None,
            "close": {"legs": legs, "quantity": k, "strategy": strat},
        }
        (rows_eq if has_stock and strat in ("Covered Call", "Covered Put", "Protective Put") else rows_opt).append(row)

    for sym, qty in remaining.items():
        if abs(qty) < 1e-9:
            continue
        q = quotes.get(sym)
        m = multiplier(sym)
        mark = _mid(q)
        prev = getattr(q, "previous_close", None)
        value = mark * qty * m if mark is not None else None
        cost = avg[sym] * qty * m
        day = (mark - prev) * qty * m if (mark is not None and prev) else None
        title, sub = _describe_single(sym, qty)
        opt = parse_option(sym)
        n = abs(qty)
        row = {
            "kind": "single", "symbol": sym, "title": title, "subtitle": sub,
            "underlying": underlying_of(sym),
            "price": round(mark, 2) if mark is not None else None,
            "price_change": round(mark - prev, 2) if (mark is not None and prev) else None,
            "price_change_pct": _pct(mark - prev, prev) if (mark is not None and prev) else None,
            "holdings": round(value, 2) if value is not None else None,
            "holdings_sub": (f"{n:g} contract{'s' if n != 1 else ''}" if opt else f"{n:g} share{'s' if n != 1 else ''}")
                            + (" short" if qty < 0 else ""),
            "cost": round(cost, 2), "cost_sub": f"${avg[sym]:.2f}/share",
            "day_return": round(day, 2) if day is not None else None,
            "day_return_pct": _pct(day, value - day) if (day is not None and value is not None) else None,
            "unrealized": round(value - cost, 2) if value is not None else None,
            "unrealized_pct": _pct(value - cost, cost) if value is not None else None,
            "close": {"legs": [{"symbol": sym, "side": "BUY" if qty > 0 else "SELL", "ratio": 1}],
                      "quantity": n, "strategy": None},
        }
        (rows_opt if opt else rows_eq).append(row)

    return {"options": rows_opt, "equities": rows_eq}


def close_position(b: PaperBroker, legs: list[dict], quantity: float, strategy: Optional[str],
                   order_type: str = "MARKET", net_price: Optional[float] = None) -> dict:
    """Reverse every leg. Single legs go through place_order, groups as one combo."""
    flip = [{"symbol": l["symbol"], "side": "SELL" if l["side"].upper() == "BUY" else "BUY",
             "ratio": l.get("ratio", 1)} for l in legs]
    if len(flip) == 1:
        l = flip[0]
        return b.place_order(l["symbol"], l["side"], quantity * l["ratio"], order_type,
                             abs(net_price) if net_price is not None else None)
    return b.place_multileg(flip, int(quantity), order_type, net_price,
                            strategy=f"Close {strategy}" if strategy else "Close position")


# ------------------------------------------------------------------ account value
RANGES = {"1D": 1, "1W": 7, "1M": 31, "3M": 92, "YTD": None, "ALL": None}


def equity_series(b: PaperBroker, rng: str = "1D") -> dict:
    rng = rng.upper() if rng.upper() in RANGES else "1D"
    now = dt.datetime.now(ET)
    rows = b.ledger.rows("SELECT ts, equity FROM equity_history ORDER BY ts")
    acct = b.account()
    start_cash = float(b.ledger.get("starting_cash"))
    if rng == "ALL":
        since = None
    elif rng == "YTD":
        since = dt.datetime(now.year, 1, 1, tzinfo=ET)
    elif rng == "1D":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        since = now - dt.timedelta(days=RANGES[rng])
    pts = [r for r in rows if since is None or dt.datetime.fromisoformat(r["ts"]) >= since]
    # baseline: last point before the window (or starting cash)
    before = [r for r in rows if since is not None and dt.datetime.fromisoformat(r["ts"]) < since]
    base = before[-1]["equity"] if before else (pts[0]["equity"] if pts and rng != "ALL" else start_cash)
    if rng == "1D":
        base = float(b.ledger.get("sod_equity", base))
    pts = pts + [{"ts": now.replace(microsecond=0).isoformat(), "equity": acct["equity"]}]
    change = acct["equity"] - base
    return {"range": rng, "points": pts, "baseline": round(base, 2), "equity": acct["equity"],
            "change": round(change, 2), "change_pct": round(change / base * 100, 2) if base else None,
            "buying_power": acct["buying_power"]}
