"""Command line for the paper account.

    python -m paper_public account
    python -m paper_public quote QQQ SPY
    python -m paper_public buy QQQ 10
    python -m paper_public buy QQQ261002C00740000 1 --limit 3.40
    python -m paper_public sell QQQ 10
    python -m paper_public positions
    python -m paper_public orders [--open]
    python -m paper_public fills
    python -m paper_public cancel <order_id>
    python -m paper_public process          # fill resting limits, expire, settle
    python -m paper_public reset --cash 25000
"""
from __future__ import annotations

import argparse
import sys

from .broker import OrderRejected, PaperBroker
from .market_data import PublicMarketData


def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "  (none)"
    w = {c: max(len(c), *(len(f"{r.get(c, '')}") for r in rows)) for c in cols}
    head = "  " + "  ".join(c.ljust(w[c]) for c in cols)
    body = ["  " + "  ".join(f"{r.get(c, '')}".ljust(w[c]) for c in cols) for r in rows]
    return "\n".join([head] + body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="paper_public")
    ap.add_argument("--db", default="paper_account.db", help="account file (default paper_account.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("account")
    q = sub.add_parser("quote"); q.add_argument("symbols", nargs="+")
    for side in ("buy", "sell"):
        s = sub.add_parser(side)
        s.add_argument("symbol"); s.add_argument("quantity", type=float)
        s.add_argument("--limit", type=float, help="limit price (omit for market)")
        s.add_argument("--gtc", action="store_true", help="good-til-cancelled instead of DAY")
    sub.add_parser("positions")
    o = sub.add_parser("orders"); o.add_argument("--open", action="store_true")
    sub.add_parser("fills")
    c = sub.add_parser("cancel"); c.add_argument("order_id")
    sub.add_parser("process")
    r = sub.add_parser("reset"); r.add_argument("--cash", type=float, default=25_000.0)
    a = ap.parse_args(argv)

    from .market_data import CachedMarketData
    data = PublicMarketData()
    b = PaperBroker(CachedMarketData(data), db_path=a.db)

    if a.cmd == "account":
        for k, v in b.account().items():
            print(f"  {k:16s} {v}")
    elif a.cmd == "quote":
        import time as _t
        t0 = _t.time()
        try:
            qs = data.quotes(a.symbols)
        except Exception as e:  # noqa: BLE001
            print(f"  quote request failed after {_t.time() - t0:.1f}s: {e}")
            return 1
        print(f"  (Public responded in {_t.time() - t0:.2f}s)")
        rows = [{"symbol": s.upper(), "bid": qs[s.upper()].bid, "ask": qs[s.upper()].ask,
                 "last": qs[s.upper()].last} for s in a.symbols if s.upper() in qs]
        print(_table(rows, ["symbol", "bid", "ask", "last"]))
    elif a.cmd in ("buy", "sell"):
        try:
            o = b.place_order(a.symbol, a.cmd.upper(), a.quantity,
                              "LIMIT" if a.limit else "MARKET", a.limit, "GTC" if a.gtc else "DAY")
        except OrderRejected as e:
            print(f"  REJECTED: {e}")
            return 1
        msg = f"@ {o['avg_fill_price']}" if o["status"] == "FILLED" else f"(resting, limit {o['limit_price']})"
        print(f"  {o['status']}: {o['side']} {o['quantity']:g} {o['symbol']} {msg}   id={o['id']}")
    elif a.cmd == "positions":
        print(_table(b.positions(), ["symbol", "quantity", "avg_cost", "mark", "market_value", "unrealized_pnl"]))
    elif a.cmd == "orders":
        print(_table(b.orders(open_only=a.open),
                     ["created_at", "symbol", "side", "quantity", "order_type", "limit_price",
                      "status", "avg_fill_price", "reason", "id"]))
    elif a.cmd == "fills":
        print(_table(b.fills(), ["ts", "symbol", "side", "quantity", "price", "fee", "realized_pnl"]))
    elif a.cmd == "cancel":
        print(f"  {b.cancel_order(a.order_id)['status']}")
    elif a.cmd == "process":
        print(f"  {b.process()}")
    elif a.cmd == "reset":
        b.reset(a.cash)
        print(f"  paper account reset to ${a.cash:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
