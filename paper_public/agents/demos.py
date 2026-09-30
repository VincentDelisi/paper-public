"""Four sample agents. They exist to show Shadow Book's execution engine at
work (market orders, brackets, multi-leg limits, order walking, spreads,
covered calls), NOT because the rules have an edge. Every decision is
written to the log in plain English.
"""
from __future__ import annotations

import datetime as dt
from collections import deque

from .base import Agent, AgentContext
from ..broker import OrderRejected
from ..instruments import parse_option


def usd(x: float, sign: bool = False) -> str:
    s = f"${abs(x):,.2f}"
    if sign:
        return ("+" if x >= 0 else "−") + s
    return ("−" if x < 0 else "") + s


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _pick_expiration(ctx: AgentContext, symbol: str, lo: int, hi: int, target: int) -> str | None:
    exps = [e for e in ctx.expirations(symbol) if lo <= ctx.dte(e) <= hi]
    return min(exps, key=lambda e: abs(ctx.dte(e) - target)) if exps else None


def _age(ctx: AgentContext, iso: str) -> float:
    return (ctx.now - dt.datetime.fromisoformat(iso)).total_seconds()


# ====================================================================== 1
class DipBuyer(Agent):
    id = "dip-buyer"
    name = "Dip Buyer"
    tagline = "Buys small pullbacks in SPY shares"
    rules = ["Tracks SPY's high for the day",
             "Buys 20 shares when SPY is 0.40% below that high",
             "Sells at +0.30% (take profit) or −0.50% (stop)",
             "Market orders · flat by 3:50 PM ET · max 3 trades a day"]
    symbols = ["SPY"]
    interval = 5.0
    SYM, QTY, DIP, TP, SL, MAX = "SPY", 20, 0.004, 0.003, 0.005, 3

    def tick(self, ctx: AgentContext) -> None:
        p = ctx.price(self.SYM)
        if p is None:
            ctx.log_every("noquote", 120, "Waiting for a SPY quote…", "info")
            return
        day = ctx.day_state()
        day["high"] = max(day.get("high", p), p)
        held = ctx.position(self.SYM)

        if held > 0:
            entry = ctx.positions()[self.SYM]["avg_cost"]
            chg = p / entry - 1
            reason = None
            if chg >= self.TP:
                reason = f"up {_pct(chg)} from the {usd(entry)} entry. Taking profit"
            elif chg <= -self.SL:
                reason = f"down {_pct(chg)} from the {usd(entry)} entry. Stop hit, cutting it"
            elif not ctx.demo and (ctx.now.hour, ctx.now.minute) >= (15, 50):
                reason = "held into 3:50 PM. Flattening before the close"
            if reason and ctx.market_open():
                ctx.log(f"SPY {usd(p)} is {reason}: selling {held:g} shares at market.", "exit")
                ctx.broker.place_order(self.SYM, "SELL", held)
                day["high"] = p                     # fresh reference after an exit
            else:
                ctx.log_every("hold", 60, f"Holding {held:g} SPY from {usd(entry)}: {_pct(chg)} "
                                          f"(take profit +0.30%, stop −0.50%).")
            return

        if day.get("trades", 0) >= self.MAX:
            ctx.log_once("max", "Made 3 trades today. Done until tomorrow.")
            return
        if not (ctx.market_open() and ctx.in_window((9, 45), (15, 30))):
            ctx.log_every("closed", 900, "Outside the trading window (9:45 AM–3:30 PM ET). Watching only.")
            return
        dip = 1 - p / day["high"]
        if dip >= self.DIP:
            ctx.log(f"SPY {usd(p)} is {dip:.2%} below today's high of {usd(day['high'])} "
                    f"(trigger 0.40%). Buying {self.QTY} shares at market.", "signal")
            ctx.broker.place_order(self.SYM, "BUY", self.QTY)
            day["trades"] = day.get("trades", 0) + 1
        else:
            ctx.log_every("watch", 45, f"SPY {usd(p)}, {dip:.2%} below today's high of "
                                       f"{usd(day['high'])}. Buys at 0.40%.")


# ====================================================================== 2
class MomentumCallScalper(Agent):
    id = "momentum-calls"
    name = "Momentum Call Scalper"
    tagline = "Buys a QQQ call on a breakout, with a bracket"
    rules = ["Watches QQQ's 10-minute high (4 minutes in demo mode)",
             "On a break above it, buys 1 at-the-money call, nearest expiration with ≥1 day left",
             "Bracket order: take-profit +25%, stop-loss −20%, whichever hits first",
             "Trades 10 AM–3 PM ET · flat by 3:45 PM · max 3 trades a day"]
    symbols = ["QQQ"]
    interval = 5.0
    SYM, TP, SL, MAX, BREAK = "QQQ", 0.25, 0.20, 3, 0.0003

    def __init__(self) -> None:
        self.samples: deque = deque()

    def reset_memory(self) -> None:
        self.samples.clear()

    def tick(self, ctx: AgentContext) -> None:
        p = ctx.price(self.SYM)
        if p is None:
            ctx.log_every("noquote", 120, "Waiting for a QQQ quote…", "info")
            return
        lookback = 240 if ctx.demo else 600
        mins = lookback // 60
        t = ctx.now.timestamp()
        prior = [x for ts, x in self.samples if t - ts <= lookback]
        covered = bool(self.samples) and t - self.samples[0][0] >= lookback * 0.9
        self.samples.append((t, p))
        while self.samples and t - self.samples[0][0] > lookback * 1.2:
            self.samples.popleft()

        calls = ctx.option_positions(self.SYM, "C")
        if calls:
            self._manage(ctx, *next(iter(calls.items())))
            return

        day = ctx.day_state()
        if day.get("trades", 0) >= self.MAX:
            ctx.log_once("max", "Made 3 trades today. Done until tomorrow.")
            return
        if not covered:
            have = (t - self.samples[0][0]) / 60
            ctx.log_every("warm", 60, f"Building price history: {have:.1f} of {mins} minutes collected "
                                      f"before it can spot a breakout.", "info")
            return
        if not (ctx.market_open() and ctx.in_window((10, 0), (15, 0))):
            ctx.log_every("closed", 900, "Outside the trading window (10 AM–3 PM ET). Watching only.")
            return
        if t - ctx.state.get("last_entry", 0) < lookback:
            ctx.log_every("cool", 60, f"Cooling down after the last trade (waits {mins} minutes).")
            return
        hi = max(prior)
        if p <= hi * (1 + self.BREAK):
            ctx.log_every("watch", 45, f"QQQ {usd(p)}; {mins}-minute high {usd(hi)}. "
                                       f"Needs a break above it.")
            return

        exp = next((e for e in ctx.expirations(self.SYM) if ctx.dte(e) >= 1), None)
        rows = ctx.chain(self.SYM, exp) if exp else []
        cands = [r for r in rows if r.get("call") and r["call"]["ask"] > 0 and r["call"]["bid"] > 0]
        if not cands:
            ctx.log_every("nochain", 120, "Breakout, but no usable option chain right now. Skipping.", "info")
            return
        row = min(cands, key=lambda r: abs(r["strike"] - p))
        c = row["call"]
        ask = c["ask"]
        tp, sl = round(ask * (1 + self.TP), 2), round(ask * (1 - self.SL), 2)
        delta = f"delta {c['delta']:.2f}, " if c.get("delta") is not None else ""
        ctx.log(f"Breakout: QQQ {usd(p)} cleared its {mins}-minute high of {usd(hi)}. Buying 1 "
                f"{ctx.fmt_opt(c['symbol'])} ({delta}{ctx.dte(exp)} DTE) at about {usd(ask)} with a bracket: "
                f"take-profit {usd(tp)} (+25%), stop-loss {usd(sl)} (−20%).", "signal")
        ctx.broker.place_order(c["symbol"], "BUY", 1, "MARKET", take_profit=tp, stop_loss=sl)
        day["trades"] = day.get("trades", 0) + 1
        ctx.state["last_entry"] = t

    def _manage(self, ctx: AgentContext, sym: str, pos: dict) -> None:
        q = ctx.quote(sym)
        if q is None:
            return
        entry, mark, qty = pos["avg_cost"], q.mid, pos["quantity"]
        chg = mark / entry - 1
        if not ctx.demo and (ctx.now.hour, ctx.now.minute) >= (15, 45) and ctx.market_open():
            for o in ctx.open_orders():
                if o["symbol"] == sym:
                    ctx.broker.cancel_order(o["id"])
            ctx.log(f"3:45 PM: closing {ctx.fmt_opt(sym)} ({_pct(chg)}) and cancelling its bracket.", "exit")
            ctx.broker.place_order(sym, "SELL", qty)
            return
        exits = {o["order_type"]: o for o in ctx.open_orders() if o["symbol"] == sym}
        tp = exits.get("LIMIT", {}).get("limit_price")
        sl = exits.get("STOP", {}).get("stop_price")
        br = f" Take-profit {usd(tp)} · stop {usd(sl)}." if tp and sl else ""
        ctx.log_every("hold", 30, f"Holding {ctx.fmt_opt(sym)} from {usd(entry)}, now {usd(mark)} "
                                  f"({_pct(chg)}).{br}")


# ====================================================================== 3
class PutCreditSpreadSeller(Agent):
    id = "put-spreads"
    name = "Put Credit Spread Seller"
    tagline = "Sells a $5-wide SPY put spread, walks the price for a fill"
    rules = ["Once a day: SPY, 5–10 days to expiration",
             "Short put near 0.25 delta, long put $5 lower (defined risk)",
             "Limit order at the mid; if unfilled, walks toward the bid in 3 steps",
             "Buys back at 50% of the credit, at 2× the credit (stop), or at 1 DTE"]
    symbols = ["SPY"]
    interval = 10.0
    SYM, WIDTH, DELTA, QTY, TAKE, STOP, MIN_CREDIT = "SPY", 5, -0.25, 1, 0.5, 2.0, 0.25

    def tick(self, ctx: AgentContext) -> None:
        if ctx.state.get("working"):
            self._work(ctx)
            return
        sp = ctx.state.get("spread")
        pos = ctx.positions()
        if sp:
            if sp["short"] in pos and sp["long"] in pos:
                self._manage(ctx, sp)
                return
            ctx.state.pop("spread")                 # closed, stopped or expired
        if ctx.option_positions(self.SYM):
            ctx.log_every("stray", 300, "Holding SPY options it didn't open as a spread. "
                                        "Waiting (use Close all to clear them).", "info")
            return
        day = ctx.day_state()
        if day.get("entered"):
            ctx.log_once("done", "Already sold today's spread. Next look tomorrow.")
            return
        if not (ctx.market_open() and ctx.in_window((10, 0), (15, 0))):
            ctx.log_every("closed", 900, "Outside the trading window (10 AM–3 PM ET). Watching only.")
            return
        self._enter(ctx, day)

    # -------------------------------------------------------------- entry
    def _enter(self, ctx: AgentContext, day: dict) -> None:
        spot = ctx.price(self.SYM)
        exp = _pick_expiration(ctx, self.SYM, 5, 10, 7)
        if spot is None or exp is None:
            ctx.log_every("noexp", 300, "No SPY expiration 5–10 days out (or no quote). Waiting.", "info")
            return
        rows = ctx.chain(self.SYM, exp)
        by_k = {r["strike"]: r["put"] for r in rows if r.get("put")}
        cands = [(k, p) for k, p in by_k.items()
                 if k < spot and p.get("delta") is not None and p["bid"] > 0]
        if not cands:
            ctx.log_every("nochain", 300, "No usable SPY put chain right now. Waiting.", "info")
            return
        k, short = min(cands, key=lambda kp: abs(kp[1]["delta"] - self.DELTA))
        long_ = by_k.get(k - self.WIDTH)
        if not long_ or long_["ask"] <= 0:
            ctx.log_every("nolong", 300, f"No ${k - self.WIDTH:g} put to buy as protection. Waiting.", "info")
            return
        mid = round(short["mid"] - long_["mid"], 2)
        nat = round(short["bid"] - long_["ask"], 2)
        if mid < self.MIN_CREDIT:
            ctx.log_every("thin", 300, f"Credit only {usd(mid)}; needs at least {usd(self.MIN_CREDIT)}. Skipping.")
            return
        e = dt.date.fromisoformat(exp)
        ctx.log(f"SPY {usd(spot)}. Selling the {e:%b} {e.day} ${k:g}/${k - self.WIDTH:g} put spread "
                f"({ctx.dte(exp)} DTE): short put delta {short['delta']:.2f}, ${self.WIDTH} wide. "
                f"Asking {usd(mid)} credit (the mid); max risk {usd((self.WIDTH - mid) * 100)}.", "signal")
        w = {"short": short["symbol"], "long": long_["symbol"], "exp": exp, "step": 0,
             "mid": mid, "nat": nat, "start_mid": mid}
        day["entered"] = True
        self._submit(ctx, w)

    def _legs(self, w: dict) -> list[dict]:
        return [{"symbol": w["short"], "side": "SELL", "ratio": 1},
                {"symbol": w["long"], "side": "BUY", "ratio": 1}]

    def _submit(self, ctx: AgentContext, w: dict) -> None:
        price = round(max(0.01, w["mid"] - (w["mid"] - w["nat"]) * w["step"] / 3), 2)
        o = ctx.broker.place_multileg(self._legs(w), self.QTY, "LIMIT", -price, strategy="Put Credit Spread")
        w.update(id=o["id"], price=price, placed=ctx.now.isoformat())
        ctx.state["working"] = w
        if o["status"] == "FILLED":
            self._filled(ctx, o)

    def _work(self, ctx: AgentContext) -> None:
        w = ctx.state["working"]
        o = ctx.broker.get_order(w["id"])
        if o is None or o["status"] in ("CANCELLED", "EXPIRED", "REJECTED"):
            ctx.log(f"Entry order {(o or {}).get('status', 'missing').lower()}. Standing down.", "info")
            ctx.state.pop("working")
            return
        if o["status"] == "FILLED":
            self._filled(ctx, o)
            return
        age = _age(ctx, w["placed"])
        every = 20 if ctx.demo else 60
        if age < every:
            return
        if w["step"] < 3:
            qs = ctx.quotes([w["short"], w["long"]])
            if len(qs) == 2:
                w["mid"] = round(qs[w["short"]].mid - qs[w["long"]].mid, 2)
                w["nat"] = round(qs[w["short"]].bid - qs[w["long"]].ask, 2)
            ctx.broker.cancel_order(o["id"])
            w["step"] += 1
            old = w["price"]
            self._submit(ctx, w)
            if ctx.state.get("working"):
                ctx.log(f"No fill at {usd(old)} after {age:.0f}s. Walking the price toward the bid "
                        f"(step {w['step']} of 3): now asking {usd(w['price'])} credit.", "order")
        elif age >= (120 if ctx.demo else 600):
            ctx.broker.cancel_order(o["id"])
            ctx.state.pop("working")
            ctx.log(f"Still no fill at {usd(w['price'])}. Cancelled; tries again tomorrow.", "info")

    def _filled(self, ctx: AgentContext, o: dict) -> None:
        w = ctx.state.pop("working")
        credit = -float(o["net_fill"])
        ctx.state["spread"] = {"short": w["short"], "long": w["long"], "exp": w["exp"],
                               "credit": credit, "qty": self.QTY}
        ctx.log(f"Spread is on for {usd(credit)} credit ({usd(credit * 100 * self.QTY)} total). Plan: buy back "
                f"at {usd(credit * (1 - self.TAKE))} (50% profit) or {usd(credit * self.STOP)} (stop), "
                f"or at 1 DTE.", "info")

    # -------------------------------------------------------------- manage
    def _manage(self, ctx: AgentContext, sp: dict) -> None:
        qs = ctx.quotes([sp["short"], sp["long"]])
        if len(qs) < 2:
            return
        mark = qs[sp["short"]].mid - qs[sp["long"]].mid
        credit, qty = sp["credit"], sp["qty"]
        pnl = (credit - mark) * 100 * qty
        dte = ctx.dte(sp["exp"])
        s, l = parse_option(sp["short"]), parse_option(sp["long"])
        label = f"SPY ${s.strike:g}/${l.strike:g} put spread"
        reason = None
        if mark <= credit * (1 - self.TAKE):
            reason = f"is worth {usd(mark)}: {1 - mark / credit:.0%} of the {usd(credit)} credit captured. Taking profit"
        elif mark >= credit * self.STOP:
            reason = f"is worth {usd(mark)}, 2× the {usd(credit)} credit. Stopping out"
        elif dte <= 1:
            reason = f"has {dte} day{'s' if dte != 1 else ''} left. Closing early to avoid expiration risk"
        if reason and ctx.market_open():
            ctx.log(f"The {label} {reason}: buying it back at market.", "exit")
            legs = [{"symbol": sp["short"], "side": "BUY", "ratio": 1},
                    {"symbol": sp["long"], "side": "SELL", "ratio": 1}]
            o = ctx.broker.place_multileg(legs, qty, "MARKET", strategy="Close Put Credit Spread")
            if o["status"] == "FILLED":
                ctx.state.pop("spread", None)
            return
        ctx.log_every("hold", 60, f"Short the {label}: sold for {usd(credit)}, now {usd(mark)} "
                                  f"({usd(pnl, True)}). Takes profit at {usd(credit * (1 - self.TAKE))}, "
                                  f"stops at {usd(credit * self.STOP)}; {dte} DTE.")


# ====================================================================== 4
class CoveredCallWriter(Agent):
    id = "covered-calls"
    name = "Covered Call Writer"
    tagline = "Owns 100 SOFI and rents out a call against it"
    rules = ["Buys 100 SOFI and sells 1 call near 0.30 delta, 21–45 days out, as one order",
             "Buys the call back once 80% of its premium is captured",
             "Then sells a fresh call against the same shares",
             "Limit orders at the natural price; unfilled orders are re-priced"]
    symbols = ["SOFI"]
    interval = 15.0
    SYM, DELTA, TAKE = "SOFI", 0.30, 0.80

    def tick(self, ctx: AgentContext) -> None:
        if ctx.state.get("working") and not self._work(ctx):
            return
        shares = ctx.position(self.SYM)
        calls = {s: r for s, r in ctx.option_positions(self.SYM, "C").items() if r["quantity"] < 0}
        if not ctx.market_open():
            ctx.log_every("closed", 900, "Market closed. Watching only.")
            return
        if calls:
            self._manage(ctx, *next(iter(calls.items())))
        elif shares >= 100:
            self._sell_call(ctx)
        elif shares == 0:
            self._open(ctx)
        else:
            ctx.log_every("odd", 300, f"Holding {shares:g} SOFI (needs 100 to cover a call). Waiting.", "info")

    def _pick_call(self, ctx: AgentContext):
        spot = ctx.price(self.SYM)
        exp = _pick_expiration(ctx, self.SYM, 21, 45, 30)
        if spot is None or exp is None:
            ctx.log_every("noexp", 300, "No SOFI expiration 21–45 days out (or no quote). Waiting.", "info")
            return None
        cands = [r["call"] for r in ctx.chain(self.SYM, exp)
                 if r.get("call") and r["strike"] > spot and r["call"]["bid"] > 0
                 and r["call"].get("delta") is not None]
        if not cands:
            ctx.log_every("nochain", 300, "No usable SOFI call chain right now. Waiting.", "info")
            return None
        return spot, exp, min(cands, key=lambda c: abs(c["delta"] - self.DELTA))

    def _open(self, ctx: AgentContext) -> None:
        pick = self._pick_call(ctx)
        sq = ctx.quote(self.SYM)
        if not pick or sq is None:
            return
        spot, exp, c = pick
        net = round(sq.ask - c["bid"], 2)
        ctx.log(f"Opening a covered call: buy 100 SOFI at about {usd(sq.ask)} and sell 1 "
                f"{ctx.fmt_opt(c['symbol'])} (delta {c['delta']:.2f}, {ctx.dte(exp)} DTE) for about "
                f"{usd(c['bid'])}. Net cost {usd(net * 100)}; the premium is {c['bid'] / sq.ask:.1%} of the "
                f"share price for {ctx.dte(exp)} days.", "signal")
        o = ctx.broker.place_multileg([{"symbol": self.SYM, "side": "BUY", "ratio": 100},
                                       {"symbol": c["symbol"], "side": "SELL", "ratio": 1}],
                                      1, "LIMIT", net, strategy="Covered Call")
        ctx.state["working"] = {"id": o["id"], "placed": ctx.now.isoformat(), "what": "covered call"}

    def _sell_call(self, ctx: AgentContext) -> None:
        pick = self._pick_call(ctx)
        if not pick:
            return
        _, exp, c = pick
        ctx.log(f"The 100 SOFI shares are uncovered. Selling 1 {ctx.fmt_opt(c['symbol'])} "
                f"(delta {c['delta']:.2f}, {ctx.dte(exp)} DTE) for {usd(c['bid'])}.", "signal")
        o = ctx.broker.place_order(c["symbol"], "SELL", 1, "LIMIT", c["bid"])
        ctx.state["working"] = {"id": o["id"], "placed": ctx.now.isoformat(), "what": "new call"}

    def _manage(self, ctx: AgentContext, sym: str, pos: dict) -> None:
        q = ctx.quote(sym)
        if q is None:
            return
        entry, mark = pos["avg_cost"], q.mid
        captured = 1 - mark / entry if entry else 0
        o = parse_option(sym)
        dte = (o.expiration - ctx.now.date()).days
        if captured >= self.TAKE:
            ctx.log(f"{ctx.fmt_opt(sym)} is worth {usd(mark)}: {captured:.0%} of the {usd(entry)} premium "
                    f"captured. Buying it back, then selling a new one.", "exit")
            oo = ctx.broker.place_order(sym, "BUY", abs(pos["quantity"]), "LIMIT", q.ask)
            ctx.state["working"] = {"id": oo["id"], "placed": ctx.now.isoformat(), "what": "buyback"}
            return
        ctx.log_every("hold", 120, f"Holding 100 SOFI + short {ctx.fmt_opt(sym)}. Sold for {usd(entry)}, "
                                   f"now {usd(mark)}: {captured:.0%} captured (buys back at 80%); {dte} DTE.")

    def _work(self, ctx: AgentContext) -> bool:
        """Returns True when the agent is free to act this tick."""
        w = ctx.state["working"]
        o = ctx.broker.get_order(w["id"])
        if o is None or o["status"] != "OPEN":
            ctx.state.pop("working")
            return True
        if _age(ctx, w["placed"]) >= (30 if ctx.demo else 90):
            ctx.broker.cancel_order(o["id"])
            ctx.state.pop("working")
            ctx.log(f"The {w['what']} order didn't fill; cancelled it and will re-price.", "order")
            return True
        return False


def default_agents() -> list[Agent]:
    return [DipBuyer(), MomentumCallScalper(), PutCreditSpreadSeller(), CoveredCallWriter()]


__all__ = ["DipBuyer", "MomentumCallScalper", "PutCreditSpreadSeller", "CoveredCallWriter",
           "default_agents", "OrderRejected"]
