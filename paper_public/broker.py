"""PaperBroker — simulated orders, fills and positions on real market data.

Design
  * Same shape as a real broker call. Single-leg: place_order(symbol, side,
    quantity, order_type, ...). Multi-leg: place_multileg(legs, quantity, ...).
    An agent written against this switches to live trading by swapping the
    broker object, not its logic.
  * Conservative fills: every leg BUYS at the ASK and SELLS at the BID. Limit
    orders fill only when the live quote reaches the price. Multi-leg orders
    fill all legs together (all-or-none) when the combined natural price is at
    or better than the net limit.
  * Positions are signed: +long, -short. Selling with no position opens a
    short (sell-to-open options, short stock).

Buying power (conservative, risk-based)
  Collateral for each underlying = the worst cash outflow at expiration over
  underlying prices from $0 to 1.5x spot, with every option at intrinsic value.
  That gives the familiar numbers exactly for the common cases:
    cash-secured put  -> strike x 100
    credit spread     -> width x 100
    covered call      -> 0 (the shares cover it)
    short stock       -> 150% of its value (Reg T style)
  Uncovered (naked) calls are capped at a 50% up-move, stricter than Reg T.

Simplifications (documented, not hidden)
  * Orders fill in full; displayed size is ignored.
  * Market hours = Mon-Fri 9:30-16:00 ET. Exchange holidays are not modeled.
  * Expiring options settle to intrinsic value in CASH (no share delivery).
  * Stop orders are checked on every process() tick (every few seconds).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import uuid
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from .instruments import is_option, multiplier, parse_option
from .ledger import Ledger
from .market_data import MarketData, Quote

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
PDT_EQUITY = 25_000.0
UPSIDE_STRESS = 1.5          # naked-call / short-stock stress: +50%

OPEN_STATUSES = ("OPEN",)
ORDER_TYPES = ("MARKET", "LIMIT", "STOP", "STOP_LIMIT")


class OrderRejected(Exception):
    pass


def _now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def underlying_of(symbol: str) -> str:
    opt = parse_option(symbol)
    return opt.underlying if opt else symbol


class PaperBroker:
    def __init__(
        self,
        data: MarketData,
        db_path: str = "paper_account.db",
        starting_cash: float = 25_000.0,
        fee_per_contract: float = 0.0,
        fee_per_share: float = 0.0,
        slippage_bps: float = 0.0,
        enforce_pdt: bool = False,   # FINRA removed the PDT rule effective 2026-06-04 (Notice 26-10)
        daily_loss_limit: Optional[float] = None,
        clock: Callable[[], dt.datetime] = _now_et,
        enforce_market_hours: bool = True,
        allow_short_stock: bool = True,
    ) -> None:
        self.data = data
        self.ledger = Ledger(db_path)
        self.fee_per_contract = fee_per_contract
        self.fee_per_share = fee_per_share
        self.slip = slippage_bps / 10_000.0
        self.enforce_pdt = enforce_pdt
        self.daily_loss_limit = daily_loss_limit
        self.clock = clock
        self.enforce_market_hours = enforce_market_hours
        self.allow_short_stock = allow_short_stock
        if self.ledger.get("cash") is None:
            self.reset(starting_cash)

    # ================================================================ account
    def reset(self, cash: float = 25_000.0) -> None:
        keep = self.ledger.get("watchlist")          # a reset clears trades, not your watchlist
        self.ledger.wipe()
        if keep is not None:
            self.ledger.set("watchlist", keep)
        self.ledger.set("cash", cash)
        self.ledger.set("starting_cash", cash)
        self.ledger.set("created_at", self.clock().isoformat())
        self.ledger.commit()

    @property
    def cash(self) -> float:
        return float(self.ledger.get("cash"))

    def _add_cash(self, amount: float) -> None:
        self.ledger.set("cash", round(self.cash + amount, 6))

    def _pos_map(self) -> dict[str, float]:
        return {r["symbol"]: float(r["quantity"])
                for r in self.ledger.rows("SELECT symbol, quantity FROM positions WHERE quantity != 0")}

    def positions(self, quotes: dict[str, Quote] | None = None) -> list[dict]:
        pos = self.ledger.rows("SELECT * FROM positions WHERE quantity != 0 ORDER BY symbol")
        if not pos:
            return []
        if quotes is None:
            quotes = self._safe_quotes(p["symbol"] for p in pos)
        out = []
        for p in pos:
            q = quotes.get(p["symbol"])
            m = multiplier(p["symbol"])
            mark = q.mid if q and q.valid else None
            mv = mark * p["quantity"] * m if mark is not None else None
            cb = p["avg_cost"] * p["quantity"] * m
            out.append({
                **p,
                "side": "LONG" if p["quantity"] > 0 else "SHORT",
                "underlying": underlying_of(p["symbol"]),
                "mark": round(mark, 4) if mark is not None else None,
                "market_value": round(mv, 2) if mv is not None else None,
                "cost_basis": round(cb, 2),
                "unrealized_pnl": round(mv - cb, 2) if mv is not None else None,
            })
        return out

    def account(self) -> dict:
        pos = self.positions()
        # Positions without a live quote are carried at cost so equity never
        # silently collapses on a data hiccup.
        pv = sum(p["market_value"] if p["market_value"] is not None else p["cost_basis"] for p in pos)
        equity = self.cash + pv
        today = self.clock().date().isoformat()
        if self.ledger.get("sod_date") != today:
            self.ledger.set("sod_date", today)
            self.ledger.set("sod_equity", equity)
            self.ledger.commit()
        sod = float(self.ledger.get("sod_equity", equity))
        realized_today = self.ledger.one(
            "SELECT COALESCE(SUM(realized_pnl),0) r FROM fills WHERE trade_date=?", (today,))["r"]
        collateral = self._requirement(self._pos_map())
        return {
            "cash": round(self.cash, 2),
            "positions_value": round(pv, 2),
            "equity": round(equity, 2),
            "collateral": round(collateral, 2),
            "buying_power": round(self._buying_power(), 2),
            "day_pnl": round(equity - sod, 2),
            "realized_today": round(realized_today, 2),
            "total_pnl": round(equity - float(self.ledger.get("starting_cash")), 2),
            "day_trades_5d": self.day_trades_count(),
            "pdt_restricted": self._pdt_blocked(equity),
        }

    # ================================================================ orders
    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
        time_in_force: str = "DAY",
        order_id: Optional[str] = None,
        stop_price: Optional[float] = None,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
    ) -> dict:
        """Single-leg order. take_profit / stop_loss turn it into a bracket:
        once the entry fills, a GTC limit (take-profit) and a GTC stop
        (stop-loss) are placed to close it; whichever fills cancels the other."""
        symbol, side = symbol.upper(), side.upper()
        order_type, tif = order_type.upper(), time_in_force.upper()
        oid = order_id or str(uuid.uuid4())
        legs = [{"symbol": symbol, "side": side, "ratio": 1}]
        rec = dict(id=oid, symbol=symbol, side=side, quantity=quantity, order_type=order_type,
                   limit_price=limit_price, time_in_force=tif, legs=json.dumps(legs),
                   order_class="BRACKET" if (take_profit or stop_loss) else "SIMPLE",
                   stop_price=stop_price, take_profit=take_profit, stop_loss=stop_loss,
                   net_limit=None)
        try:
            self._validate_simple(symbol, side, quantity, order_type, limit_price, stop_price, tif,
                                  take_profit, stop_loss)
            reserved = self._check_risk(legs, quantity, self._est_leg_prices(legs, order_type, limit_price, stop_price))
        except OrderRejected as e:
            self._insert(rec, "REJECTED", str(e))
            self.ledger.commit()
            raise
        rec["reserved"] = reserved
        self._insert(rec, "OPEN", None)
        self.ledger.commit()
        self._try_fill(self.get_order(oid))
        self.ledger.commit()
        return self.get_order(oid)

    def place_multileg(
        self,
        legs: list[dict],
        quantity: int,
        order_type: str = "LIMIT",
        net_price: Optional[float] = None,
        time_in_force: str = "DAY",
        strategy: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> dict:
        """Multi-leg order filled all-or-none.

        legs: [{"symbol": OSI or stock, "side": "BUY"|"SELL", "ratio": 1}, ...]
              (stock legs use ratio 100 per spread, e.g. covered call)
        net_price: per-share net for LIMIT orders, signed: + = pay (debit),
                   - = receive (credit). Fills when the natural net is <= it.
        """
        order_type, tif = order_type.upper(), time_in_force.upper()
        oid = order_id or str(uuid.uuid4())
        norm = [{"symbol": l["symbol"].upper(), "side": l["side"].upper(),
                 "ratio": float(l.get("ratio", 1))} for l in legs]
        und = sorted({underlying_of(l["symbol"]) for l in norm})
        rec = dict(id=oid, symbol=und[0] if len(und) == 1 else "/".join(und),
                   side="DEBIT" if (net_price or 0) >= 0 else "CREDIT", quantity=quantity,
                   order_type=order_type, limit_price=abs(net_price) if net_price is not None else None,
                   time_in_force=tif, legs=json.dumps(norm), order_class="MULTILEG",
                   strategy=strategy, net_limit=net_price, stop_price=None,
                   take_profit=None, stop_loss=None)
        try:
            self._validate_multileg(norm, quantity, order_type, net_price, tif)
            est = self._est_leg_prices(norm, "MARKET" if order_type == "MARKET" else "NET", None, None,
                                       net_price=net_price)
            if net_price is None:                 # market: label by the natural net
                rec["side"] = "DEBIT" if self._net_per_unit(norm, est) >= 0 else "CREDIT"
            reserved = self._check_risk(norm, quantity, est)
        except OrderRejected as e:
            self._insert(rec, "REJECTED", str(e))
            self.ledger.commit()
            raise
        rec["reserved"] = reserved
        self._insert(rec, "OPEN", None)
        self.ledger.commit()
        self._try_fill(self.get_order(oid))
        self.ledger.commit()
        return self.get_order(oid)

    def cancel_order(self, order_id: str) -> dict:
        o = self.get_order(order_id)
        if o is None:
            raise KeyError(order_id)
        if o["status"] in OPEN_STATUSES:
            self._set_status(order_id, "CANCELLED", "cancelled by user")
            self.ledger.commit()
        return self.get_order(order_id)

    def get_order(self, order_id: str) -> dict | None:
        return self._decode(self.ledger.one("SELECT * FROM orders WHERE id=?", (order_id,)))

    def orders(self, open_only: bool = False, limit: int = 50) -> list[dict]:
        where = "WHERE status='OPEN'" if open_only else ""
        return [self._decode(o) for o in self.ledger.rows(
            f"SELECT * FROM orders {where} ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]

    def fills(self, limit: int = 50) -> list[dict]:
        return self.ledger.rows("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))

    @staticmethod
    def _decode(o: dict | None) -> dict | None:
        if o is None:
            return None
        o = dict(o)
        o["legs"] = json.loads(o["legs"]) if o.get("legs") else [
            {"symbol": o["symbol"], "side": o["side"], "ratio": 1}]
        return o

    # ================================================================ loop
    def process(self) -> dict:
        """Call on a timer (every few seconds). Triggers stops, fills resting
        orders whose price is reached, expires DAY orders after the close,
        settles expired options and records equity history."""
        now = self.clock()
        filled = expired = 0
        open_orders = self.orders(open_only=True, limit=10_000)
        if open_orders:
            syms = {l["symbol"] for o in open_orders for l in o["legs"]}
            quotes = self._safe_quotes(syms)
            for o in open_orders:
                if self.get_order(o["id"])["status"] != "OPEN":
                    continue        # cancelled by an OCO sibling this tick
                created = dt.datetime.fromisoformat(o["created_at"])
                if o["time_in_force"] == "DAY" and self._day_over(created, now):
                    self._set_status(o["id"], "EXPIRED", "day order expired at close")
                    expired += 1
                    continue
                if any((parse_option(l["symbol"]) and parse_option(l["symbol"]).expiration < now.date())
                       for l in o["legs"]):
                    self._set_status(o["id"], "EXPIRED", "contract expired")
                    expired += 1
                    continue
                if self._try_fill(o, quotes):
                    filled += 1
        settled = self._settle_expirations(now)
        self._record_equity(now)
        self.ledger.commit()
        return {"filled": filled, "expired": expired, "settled": settled}

    # ================================================================ validation
    def _validate_common(self, tif: str) -> None:
        if tif not in ("DAY", "GTC"):
            raise OrderRejected("time_in_force must be DAY or GTC")

    def _validate_simple(self, symbol, side, quantity, order_type, limit_price, stop_price, tif,
                         take_profit, stop_loss) -> None:
        self._validate_common(tif)
        if side not in ("BUY", "SELL"):
            raise OrderRejected("side must be BUY or SELL")
        if order_type not in ORDER_TYPES:
            raise OrderRejected(f"order_type must be one of {', '.join(ORDER_TYPES)}")
        if quantity is None or quantity <= 0:
            raise OrderRejected("quantity must be positive")
        if is_option(symbol) and float(quantity) != int(quantity):
            raise OrderRejected("option quantity must be a whole number of contracts")
        if order_type in ("LIMIT", "STOP_LIMIT") and (limit_price is None or limit_price <= 0):
            raise OrderRejected(f"{order_type} orders need a positive limit price")
        if order_type in ("STOP", "STOP_LIMIT") and (stop_price is None or stop_price <= 0):
            raise OrderRejected(f"{order_type} orders need a positive stop price")
        if order_type == "MARKET" and self.enforce_market_hours and not self._market_open(self.clock()):
            raise OrderRejected("market is closed; use a LIMIT order to rest until the open")
        opt = parse_option(symbol)
        if opt and opt.expiration < self.clock().date():
            raise OrderRejected(f"option expired on {opt.expiration}")
        if take_profit or stop_loss:
            if self._position_qty(symbol) != 0:
                raise OrderRejected("brackets attach to a new position; this symbol already has one")
            ref = limit_price or stop_price
            if ref is None:
                q = self._safe_quotes([symbol]).get(symbol)
                ref = (q.ask if side == "BUY" else q.bid) if q and q.valid else None
            if ref:
                long_ = side == "BUY"
                if take_profit and ((long_ and take_profit <= ref) or (not long_ and take_profit >= ref)):
                    raise OrderRejected("take-profit must be above the entry for a buy, below for a sell")
                if stop_loss and ((long_ and stop_loss >= ref) or (not long_ and stop_loss <= ref)):
                    raise OrderRejected("stop-loss must be below the entry for a buy, above for a sell")
        self._validate_leg_effect(symbol, side, float(quantity))

    def _validate_multileg(self, legs, quantity, order_type, net_price, tif) -> None:
        self._validate_common(tif)
        if not 2 <= len(legs) <= 4:
            raise OrderRejected("multi-leg orders need 2 to 4 legs")
        if order_type not in ("MARKET", "LIMIT"):
            raise OrderRejected("multi-leg orders are MARKET or LIMIT")
        if order_type == "LIMIT" and net_price is None:
            raise OrderRejected("LIMIT multi-leg orders need a net price")
        if quantity is None or quantity <= 0 or float(quantity) != int(quantity):
            raise OrderRejected("quantity must be a whole number")
        if len({l["symbol"] for l in legs}) != len(legs):
            raise OrderRejected("each leg must be a different contract")
        if order_type == "MARKET" and self.enforce_market_hours and not self._market_open(self.clock()):
            raise OrderRejected("market is closed; use a LIMIT order to rest until the open")
        for l in legs:
            if l["side"] not in ("BUY", "SELL") or l["ratio"] <= 0:
                raise OrderRejected("each leg needs side BUY/SELL and a positive ratio")
            opt = parse_option(l["symbol"])
            if opt and opt.expiration < self.clock().date():
                raise OrderRejected(f"{l['symbol']} expired on {opt.expiration}")
            self._validate_leg_effect(l["symbol"], l["side"], l["ratio"] * quantity)

    def _validate_leg_effect(self, symbol: str, side: str, qty: float) -> None:
        """A leg may open, add to, reduce or close a position, but not flip it
        from long to short (or back) in one go."""
        held = self._position_qty(symbol)
        delta = qty if side == "BUY" else -qty
        pending = self._pending_delta(symbol, closing_for=held)
        if held > 0 and delta < 0 and qty + pending > held + 1e-9:
            raise OrderRejected(f"{symbol}: sell quantity exceeds position ({held:g} held, "
                                f"{pending:g} already pending to close)")
        if held < 0 and delta > 0 and qty + pending > -held + 1e-9:
            raise OrderRejected(f"{symbol}: buy quantity exceeds short position ({-held:g} short, "
                                f"{pending:g} already pending to close)")
        if held == 0 and delta < 0 and not is_option(symbol) and not self.allow_short_stock:
            raise OrderRejected("short selling stock is disabled")

    def _pending_delta(self, symbol: str, closing_for: float) -> float:
        """Quantity already committed to closing `symbol` by resting orders.
        Bracket siblings (take-profit + stop-loss) count once."""
        if closing_for == 0:
            return 0.0
        closing_side = "SELL" if closing_for > 0 else "BUY"
        seen_groups, total = set(), 0.0
        for o in self.orders(open_only=True, limit=10_000):
            for l in o["legs"]:
                if l["symbol"] == symbol and l["side"] == closing_side:
                    if o.get("oco_group"):
                        if o["oco_group"] in seen_groups:
                            continue
                        seen_groups.add(o["oco_group"])
                    total += l["ratio"] * o["quantity"]
        return total

    # ================================================================ risk
    def _est_leg_prices(self, legs, order_type, limit_price, stop_price, net_price=None) -> dict[str, float]:
        """Price each leg would likely fill at, for the buying-power check."""
        if len(legs) == 1 and order_type in ("LIMIT", "STOP_LIMIT"):
            return {legs[0]["symbol"]: float(limit_price)}
        if len(legs) == 1 and order_type == "STOP":
            return {legs[0]["symbol"]: float(stop_price)}
        quotes = self._safe_quotes(l["symbol"] for l in legs)
        est = {}
        for l in legs:
            q = quotes.get(l["symbol"])
            if not q or not q.valid:
                raise OrderRejected(f"no valid quote for {l['symbol']}")
            if order_type == "MARKET" and getattr(q, "stale", False):
                raise OrderRejected("prices are delayed (Public API slow); try again in a few "
                                    "seconds or use a limit order")
            est[l["symbol"]] = (q.ask * (1 + self.slip)) if l["side"] == "BUY" else (q.bid * (1 - self.slip))
        if order_type == "NET" and net_price is not None:
            # Scale natural prices so the package costs exactly the net limit.
            nat = self._net_per_unit(legs, est)
            shift = net_price - nat
            buy_units = sum(l["ratio"] * multiplier(l["symbol"]) / 100 for l in legs if l["side"] == "BUY") or 1
            for l in legs:
                if l["side"] == "BUY":
                    est[l["symbol"]] = max(0.0, est[l["symbol"]] + shift / buy_units)
        return est

    @staticmethod
    def _net_per_unit(legs, prices: dict[str, float]) -> float:
        """Signed net per spread in per-share dollars: + pay (debit), - receive (credit)."""
        tot = 0.0
        for l in legs:
            sign = 1 if l["side"] == "BUY" else -1
            tot += sign * prices[l["symbol"]] * l["ratio"] * multiplier(l["symbol"]) / 100
        return tot

    def _check_risk(self, legs, quantity, est: dict[str, float]) -> float:
        """Reject if the order would leave negative buying power (or make an
        already-negative one worse). Returns the buying power to reserve while
        the order rests."""
        pos = self._pos_map()
        after = dict(pos)
        cash_delta = 0.0
        opens = False
        for l in legs:
            q = l["ratio"] * quantity
            d = q if l["side"] == "BUY" else -q
            held = after.get(l["symbol"], 0.0)
            if held == 0 or (held > 0) == (d > 0):
                opens = True
            after[l["symbol"]] = held + d
            m = multiplier(l["symbol"])
            cash_delta += -d * est[l["symbol"]] * m - self._fee(l["symbol"], q)
        if opens:
            if self.daily_loss_limit is not None:
                acct_day = self.account()["day_pnl"]
                if acct_day <= -abs(self.daily_loss_limit):
                    raise OrderRejected(f"daily loss limit hit ({acct_day:.2f}); no new positions today")
            if self.enforce_pdt and self._pdt_blocked(self.account()["equity"]):
                raise OrderRejected("PDT: 3 day trades in 5 business days with equity under $25,000")
        bp_now = self._buying_power(pos)
        bp_after = self.cash + cash_delta - self._requirement(after) - self._reserved_total()
        if bp_after < min(0.0, bp_now) - 1e-6:
            need = bp_now - bp_after
            raise OrderRejected(f"insufficient buying power: this order needs ${need:,.2f}, "
                                f"you have ${max(bp_now, 0):,.2f}")
        return round(max(0.0, bp_now - bp_after), 2)

    def _buying_power(self, pos: dict[str, float] | None = None) -> float:
        pos = self._pos_map() if pos is None else pos
        return self.cash - self._requirement(pos) - self._reserved_total()

    def _reserved_total(self) -> float:
        r = self.ledger.one("SELECT COALESCE(SUM(reserved),0) r FROM orders WHERE status='OPEN'")
        return float(r["r"] or 0)

    def _requirement(self, pos: dict[str, float]) -> float:
        """Collateral per underlying = worst settlement shortfall over prices
        0 .. 1.5x spot (options at intrinsic, shares at price)."""
        groups: dict[str, list[tuple[str, float]]] = {}
        for sym, q in pos.items():
            if q:
                groups.setdefault(underlying_of(sym), []).append((sym, q))
        need = 0.0
        spots = None
        for und, items in groups.items():
            if all(q > 0 for _, q in items):
                continue                        # nothing short: no collateral
            if spots is None:
                spots = self._safe_quotes(groups.keys())
            sq = spots.get(und)
            strikes = [parse_option(s).strike for s, _ in items if is_option(s)]
            spot = (sq.last or sq.mid) if sq and (sq.last or sq.valid) else (max(strikes) if strikes else None)
            if spot is None:
                # No price at all: hold the full short-stock notional at cost.
                spot = max((abs(q) for _, q in items), default=0)
            grid = sorted({0.0, spot, spot * UPSIDE_STRESS, *strikes})
            grid = [p for p in grid if p <= spot * UPSIDE_STRESS + 1e-9]

            def value(P: float) -> float:
                v = 0.0
                for s, q in items:
                    o = parse_option(s)
                    if o:
                        intr = max(P - o.strike, 0.0) if o.right == "C" else max(o.strike - P, 0.0)
                        v += q * 100 * intr
                    else:
                        v += q * P
                return v
            need += max(0.0, -min(value(P) for P in grid))
        return need

    # ================================================================ fills
    def _try_fill(self, o: dict, quotes: dict[str, Quote] | None = None) -> bool:
        if o is None or o["status"] != "OPEN":
            return False
        legs = o["legs"]
        if self.enforce_market_hours and not self._market_open(self.clock()):
            if o["order_type"] in ("MARKET", "STOP", "STOP_LIMIT") or len(legs) > 1:
                return False
        if quotes is None:
            quotes = self._safe_quotes(l["symbol"] for l in legs)
        qs = [quotes.get(l["symbol"]) for l in legs]
        if any(q is None or not q.valid or getattr(q, "stale", False) for q in qs):
            return False                      # never fill on a missing or old price
        nat = {l["symbol"]: (q.ask * (1 + self.slip) if l["side"] == "BUY" else q.bid * (1 - self.slip))
               for l, q in zip(legs, qs)}

        otype = o["order_type"]
        if otype in ("STOP", "STOP_LIMIT") and o.get("trigger_state") != "TRIGGERED":
            q, buy = qs[0], legs[0]["side"] == "BUY"
            hit = (q.ask >= o["stop_price"]) if buy else (q.bid <= o["stop_price"])
            if not hit:
                return False
            self.ledger.execute("UPDATE orders SET trigger_state='TRIGGERED', updated_at=? WHERE id=?",
                                (self.clock().isoformat(), o["id"]))
        if otype == "STOP_LIMIT":
            otype = "LIMIT"
        elif otype == "STOP":
            otype = "MARKET"

        if len(legs) == 1:
            sym, buy = legs[0]["symbol"], legs[0]["side"] == "BUY"
            price = nat[sym]
            if otype == "LIMIT":
                lim = o["limit_price"]
                if (buy and price > lim) or (not buy and price < lim):
                    return False
            self._fill_order(o, {sym: round(price, 4)})
            return True

        net = self._net_per_unit(legs, nat)
        if otype == "LIMIT" and net > o["net_limit"] + 1e-9:
            return False
        self._fill_order(o, {s: round(p, 4) for s, p in nat.items()})
        return True

    def _fill_order(self, o: dict, prices: dict[str, float]) -> None:
        now = self.clock()
        for l in o["legs"]:
            self._apply_fill(o["id"], l["symbol"], l["side"], l["ratio"] * o["quantity"],
                             prices[l["symbol"]], now)
        net = self._net_per_unit(o["legs"], prices)
        avg = prices[o["legs"][0]["symbol"]] if len(o["legs"]) == 1 else abs(net)
        self.ledger.execute(
            "UPDATE orders SET status='FILLED', filled_quantity=?, avg_fill_price=?, net_fill=?, "
            "reserved=0, updated_at=? WHERE id=?",
            (o["quantity"], round(avg, 4), round(net, 4), now.isoformat(), o["id"]))
        if len(o["legs"]) > 1:
            self.ledger.execute("UPDATE orders SET side=? WHERE id=?",
                                ("DEBIT" if net >= 0 else "CREDIT", o["id"]))
        if o.get("oco_group"):
            for sib in self.ledger.rows("SELECT id FROM orders WHERE oco_group=? AND id!=? AND status='OPEN'",
                                        (o["oco_group"], o["id"])):
                self._set_status(sib["id"], "CANCELLED", "other side of bracket filled")
        if o.get("take_profit") or o.get("stop_loss"):
            self._spawn_bracket(o)
        self._record_equity(now, force=True)

    def _spawn_bracket(self, parent: dict) -> None:
        leg = parent["legs"][0]
        exit_side = "SELL" if leg["side"] == "BUY" else "BUY"
        group = str(uuid.uuid4())
        base = dict(symbol=leg["symbol"], side=exit_side, quantity=parent["quantity"],
                    time_in_force="GTC", legs=json.dumps([{"symbol": leg["symbol"], "side": exit_side, "ratio": 1}]),
                    order_class="BRACKET_EXIT", parent_id=parent["id"], oco_group=group,
                    take_profit=None, stop_loss=None, net_limit=None, reserved=0)
        if parent.get("take_profit"):
            self._insert({**base, "id": str(uuid.uuid4()), "order_type": "LIMIT",
                          "limit_price": parent["take_profit"], "stop_price": None}, "OPEN", "take-profit")
        if parent.get("stop_loss"):
            self._insert({**base, "id": str(uuid.uuid4()), "order_type": "STOP",
                          "limit_price": None, "stop_price": parent["stop_loss"]}, "OPEN", "stop-loss")

    def _apply_fill(self, order_id: str, sym: str, side: str, qty: float, price: float,
                    now: dt.datetime, effect: Optional[str] = None) -> float:
        m = multiplier(sym)
        fee = self._fee(sym, qty)
        pos = self.ledger.one("SELECT * FROM positions WHERE symbol=?", (sym,))
        held = float(pos["quantity"]) if pos else 0.0
        delta = qty if side == "BUY" else -qty
        realized = 0.0
        if held == 0 or (held > 0) == (delta > 0):
            new_q = held + delta
            avg = ((abs(held) * pos["avg_cost"]) + qty * price) / abs(new_q) if pos else price
            if pos:
                self.ledger.execute("UPDATE positions SET quantity=?, avg_cost=? WHERE symbol=?",
                                    (new_q, avg, sym))
            else:
                self.ledger.execute("INSERT INTO positions(symbol,quantity,avg_cost,opened_at) VALUES(?,?,?,?)",
                                    (sym, new_q, price, now.isoformat()))
            realized = -fee
            eff = effect or "OPEN"
        else:
            closing = min(qty, abs(held))
            if held > 0:
                realized = (price - pos["avg_cost"]) * closing * m - fee
            else:
                realized = (pos["avg_cost"] - price) * closing * m - fee
            new_q = held + delta
            if abs(new_q) <= 1e-9:
                self.ledger.execute("DELETE FROM positions WHERE symbol=?", (sym,))
            else:
                self.ledger.execute("UPDATE positions SET quantity=? WHERE symbol=?", (new_q, sym))
            eff = effect or "CLOSE"
            if self._opened_today(sym, now):
                self.ledger.execute("INSERT INTO day_trades(trade_date,symbol,order_id) VALUES(?,?,?)",
                                    (now.date().isoformat(), sym, order_id))
        self._add_cash(-delta * price * m - fee)
        self.ledger.execute(
            "INSERT INTO fills(order_id,ts,trade_date,symbol,side,quantity,price,fee,realized_pnl,effect) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (order_id, now.isoformat(), now.date().isoformat(), sym, side, qty, price, fee,
             round(realized, 6), eff))
        return realized

    def _settle_expirations(self, now: dt.datetime) -> int:
        settled = 0
        for p in self.ledger.rows("SELECT * FROM positions"):
            opt = parse_option(p["symbol"])
            if not opt:
                continue
            after_close = now.date() > opt.expiration or (
                now.date() == opt.expiration and (now.hour, now.minute) >= (16, 0))
            if not after_close:
                continue
            uq = self._safe_quotes([opt.underlying]).get(opt.underlying)
            spot = (uq.last if uq and uq.last else (uq.mid if uq and uq.valid else None))
            if spot is None:
                log.warning("cannot settle %s: no underlying price", p["symbol"])
                continue
            intrinsic = max(spot - opt.strike, 0.0) if opt.right == "C" else max(opt.strike - spot, 0.0)
            qty = p["quantity"]                     # signed: shorts pay intrinsic
            realized = (intrinsic - p["avg_cost"]) * qty * 100
            self._add_cash(intrinsic * qty * 100)
            self.ledger.execute("DELETE FROM positions WHERE symbol=?", (p["symbol"],))
            self.ledger.execute(
                "INSERT INTO fills(order_id,ts,trade_date,symbol,side,quantity,price,fee,realized_pnl,effect) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("EXPIRATION", now.isoformat(), opt.expiration.isoformat(), p["symbol"],
                 "EXPIRE", abs(qty), round(intrinsic, 4), 0.0, round(realized, 6),
                 "ASSIGNED" if intrinsic > 0 and qty < 0 else ("EXERCISED" if intrinsic > 0 else "EXPIRED")))
            for o in self.orders(open_only=True, limit=10_000):
                if any(l["symbol"] == p["symbol"] for l in o["legs"]):
                    self._set_status(o["id"], "CANCELLED", "contract expired")
            settled += 1
        return settled

    # ================================================================ analytics
    def _record_equity(self, now: dt.datetime, force: bool = False) -> None:
        last = self.ledger.one("SELECT ts FROM equity_history ORDER BY ts DESC LIMIT 1")
        if not force and last:
            gap = (now - dt.datetime.fromisoformat(last["ts"])).total_seconds()
            if gap < (60 if self._market_open(now) else 1800):
                return
        try:
            eq = self.account()["equity"]
        except Exception:  # noqa: BLE001
            return
        self.ledger.execute("INSERT OR REPLACE INTO equity_history(ts,equity) VALUES(?,?)",
                            (now.replace(microsecond=0).isoformat(), eq))

    def analytics(self) -> dict:
        closes = self.ledger.rows(
            "SELECT * FROM fills WHERE effect IN ('CLOSE','ASSIGNED','EXERCISED','EXPIRED') ORDER BY id")
        pnl = [c["realized_pnl"] for c in closes]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x <= 0]
        by_und: dict[str, dict] = {}
        for c in closes:
            u = underlying_of(c["symbol"])
            d = by_und.setdefault(u, {"underlying": u, "trades": 0, "wins": 0, "realized": 0.0})
            d["trades"] += 1
            d["wins"] += c["realized_pnl"] > 0
            d["realized"] = round(d["realized"] + c["realized_pnl"], 2)
        acct = self.account()
        start = float(self.ledger.get("starting_cash"))
        hist = self.ledger.rows("SELECT ts, equity FROM equity_history ORDER BY ts")
        peak, max_dd = start, 0.0
        for h in hist:
            peak = max(peak, h["equity"])
            max_dd = max(max_dd, peak - h["equity"])
        return {
            "closed_trades": len(pnl),
            "win_rate": round(len(wins) / len(pnl) * 100, 1) if pnl else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "realized_total": round(sum(pnl), 2),
            "best": round(max(pnl), 2) if pnl else None,
            "worst": round(min(pnl), 2) if pnl else None,
            "total_return_pct": round((acct["equity"] - start) / start * 100, 2) if start else None,
            "max_drawdown": round(max_dd, 2),
            "starting_cash": start,
            "equity": acct["equity"],
            "by_underlying": sorted(by_und.values(), key=lambda d: -abs(d["realized"])),
            "equity_curve": hist,
        }

    # ================================================================ helpers
    def _insert(self, rec: dict, status: str, reason: Optional[str]) -> None:
        now = self.clock().isoformat()
        cols = ["id", "created_at", "updated_at", "symbol", "side", "quantity", "order_type", "limit_price",
                "time_in_force", "status", "reason", "legs", "order_class", "strategy", "stop_price",
                "trigger_state", "parent_id", "oco_group", "take_profit", "stop_loss", "reserved",
                "net_limit"]
        vals = {**{c: None for c in cols}, **rec, "created_at": now, "updated_at": now,
                "status": status, "reason": reason}
        if status != "OPEN":
            vals["reserved"] = 0
        self.ledger.execute(f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            tuple(vals.get(c) for c in cols))

    def _set_status(self, oid: str, status: str, reason: str) -> None:
        self.ledger.execute("UPDATE orders SET status=?, reason=?, reserved=0, updated_at=? WHERE id=?",
                            (status, reason, self.clock().isoformat(), oid))

    def _position_qty(self, symbol: str) -> float:
        r = self.ledger.one("SELECT quantity FROM positions WHERE symbol=?", (symbol,))
        return float(r["quantity"]) if r else 0.0

    def _fee(self, symbol: str, qty: float) -> float:
        return (self.fee_per_contract if is_option(symbol) else self.fee_per_share) * qty

    def _opened_today(self, symbol: str, now: dt.datetime) -> bool:
        return self.ledger.one("SELECT 1 FROM fills WHERE symbol=? AND effect='OPEN' AND trade_date=?",
                               (symbol, now.date().isoformat())) is not None

    def day_trades_count(self) -> int:
        days = self._last_business_days(self.clock().date(), 5)
        r = self.ledger.one(
            f"SELECT COUNT(*) n FROM day_trades WHERE trade_date IN ({','.join('?' * len(days))})",
            tuple(d.isoformat() for d in days))
        return int(r["n"])

    def _pdt_blocked(self, equity: float) -> bool:
        return self.enforce_pdt and equity < PDT_EQUITY and self.day_trades_count() >= 3

    @staticmethod
    def _last_business_days(today: dt.date, n: int) -> list[dt.date]:
        out, d = [], today
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d -= dt.timedelta(days=1)
        return out

    def market_open(self) -> bool:
        return self._market_open(self.clock())

    @staticmethod
    def _market_open(now: dt.datetime) -> bool:
        now = now.astimezone(ET)
        return now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)

    @staticmethod
    def _session_date(t: dt.datetime) -> dt.date:
        """Orders placed after the close or on a weekend belong to the next
        weekday's session."""
        t = t.astimezone(ET)
        d = t.date()
        if t.weekday() >= 5 or (t.hour, t.minute) >= (16, 0):
            d += dt.timedelta(days=1)
            while d.weekday() >= 5:
                d += dt.timedelta(days=1)
        return d

    @classmethod
    def _day_over(cls, created: dt.datetime, now: dt.datetime) -> bool:
        """DAY orders expire at 16:00 ET of their session."""
        sess = cls._session_date(created)
        n = now.astimezone(ET)
        return n.date() > sess or (n.date() == sess and (n.hour, n.minute) >= (16, 0))

    def _safe_quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        syms = sorted(set(symbols))
        if not syms:
            return {}
        try:
            return self.data.quotes(syms)
        except Exception as e:  # noqa: BLE001
            log.warning("quote fetch failed: %s", e)
            return {}
