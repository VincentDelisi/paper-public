"""Runs the agents. Each agent trades its own $25k paper book (a separate
SQLite file), so their results never mix with yours or each other's.

    runner = AgentRunner(data, Path("agents"), demo=False)
    runner.start("dip-buyer")
    runner.step()        # call every few seconds (the server does this)

Agents start paused every time the server starts. Pausing stops new
decisions; open positions and resting orders (brackets) stay in place and
keep being processed.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..broker import OrderRejected, PaperBroker, _now_et
from ..instruments import parse_option
from .. import portfolio as pf
from .base import Agent, AgentContext
from .demos import default_agents, usd

log = logging.getLogger(__name__)


@dataclass
class Slot:
    agent: Agent
    broker: PaperBroker
    enabled: bool = False
    last_tick: float = 0.0
    cache: dict = field(default_factory=dict)


class AgentRunner:
    def __init__(self, data, folder: Path, demo: bool, agents: Optional[list[Agent]] = None,
                 clock: Callable[[], dt.datetime] = _now_et, starting_cash: float = 25_000.0) -> None:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.data, self.demo, self.starting_cash = data, demo, starting_cash
        self.lock = threading.RLock()
        self.slots: dict[str, Slot] = {}
        for a in agents if agents is not None else default_agents():
            b = PaperBroker(data, db_path=str(folder / f"{a.id}.db"), starting_cash=starting_cash,
                            clock=clock, enforce_market_hours=not demo)
            self.slots[a.id] = Slot(a, b)

    # ------------------------------------------------------------ control
    def _slot(self, agent_id: str) -> Slot:
        if agent_id not in self.slots:
            raise KeyError(agent_id)
        return self.slots[agent_id]

    def _ctx(self, s: Slot) -> AgentContext:
        return AgentContext(s.broker, self.data, self.demo, s.cache)

    def _system(self, s: Slot, msg: str) -> None:
        self._ctx(s).log(msg, "system")

    def start(self, agent_id: str) -> None:
        with self.lock:
            s = self._slot(agent_id)
            if not s.enabled:
                s.enabled, s.last_tick = True, 0.0
                self._system(s, f"Started. {s.agent.tagline}.")

    def stop(self, agent_id: str) -> None:
        with self.lock:
            s = self._slot(agent_id)
            if s.enabled:
                s.enabled = False
                self._system(s, "Paused. Open positions and resting orders stay in place.")

    def start_all(self) -> None:
        for k in self.slots:
            self.start(k)

    def stop_all(self) -> None:
        for k in self.slots:
            self.stop(k)

    def reset(self, agent_id: str) -> None:
        with self.lock:
            s = self._slot(agent_id)
            s.enabled = False
            s.broker.reset(self.starting_cash)
            s.agent.reset_memory()
            s.cache.clear()
            self._system(s, f"Book reset to {usd(self.starting_cash)}. Paused.")

    def flatten(self, agent_id: str) -> dict:
        """Pause the agent, cancel its orders and close every position at market."""
        with self.lock:
            s = self._slot(agent_id)
            s.enabled = False
            b = s.broker
            for o in b.orders(open_only=True, limit=1000):
                b.cancel_order(o["id"])
            closed, errors = 0, []
            # shorts first: buying back a short call before selling the shares
            # that cover it never leaves a naked position in between
            for p in sorted(b.ledger.rows("SELECT * FROM positions WHERE quantity != 0"),
                            key=lambda r: r["quantity"]):
                try:
                    o = b.place_order(p["symbol"], "SELL" if p["quantity"] > 0 else "BUY", abs(p["quantity"]))
                    closed += o["status"] == "FILLED"
                except OrderRejected as e:
                    errors.append(f"{p['symbol']}: {e}")
            ctx = self._ctx(s)
            ctx.state.pop("working", None)
            ctx.state.pop("spread", None)
            ctx.save()
            ctx.log(f"Closed everything on request ({closed} position{'s' if closed != 1 else ''})"
                    + (f"; could not close {', '.join(errors)}" if errors else "") + ". Paused.", "system")
            self._log_fills(s, ctx)
            return {"closed": closed, "errors": errors}

    # ------------------------------------------------------------ loop
    def step(self, now: Optional[float] = None) -> None:
        """One pass: process every book (fills, brackets, expirations), log
        new fills, then let each running agent decide if it's due."""
        t = time.time() if now is None else now
        for s in self.slots.values():
            with self.lock:
                ctx = None
                try:
                    s.broker.process()
                    ctx = self._ctx(s)
                    self._log_fills(s, ctx)
                    if s.enabled and t - s.last_tick >= s.agent.interval:
                        s.last_tick = t
                        s.agent.tick(ctx)
                        self._log_fills(s, ctx)
                    ctx.save()
                except OrderRejected as e:
                    self._error(s, ctx, f"Order rejected: {e}")
                except Exception as e:  # noqa: BLE001
                    log.exception("agent %s", s.agent.id)
                    self._error(s, ctx, f"Error: {e}")

    def _error(self, s: Slot, ctx: Optional[AgentContext], msg: str) -> None:
        try:
            ctx = ctx or self._ctx(s)
            ctx.log_every("error:" + msg[:60], 120, msg, "error")
            ctx.save()
        except Exception:  # noqa: BLE001
            log.exception("could not log agent error")

    def _log_fills(self, s: Slot, ctx: AgentContext) -> None:
        b = s.broker
        last = int(b.ledger.get("agent_last_fill", 0) or 0)
        new = b.ledger.rows("SELECT * FROM fills WHERE id > ? ORDER BY id", (last,))
        if not new:
            return
        groups: dict[str, list[dict]] = {}
        for f in new:
            groups.setdefault(f["order_id"], []).append(f)
        for oid, fs in groups.items():
            closes = [f for f in fs if f["effect"] != "OPEN"]
            pnl = sum(f["realized_pnl"] or 0 for f in closes)
            if oid == "EXPIRATION":
                for f in fs:
                    what = {"EXPIRED": "expired worthless", "ASSIGNED": f"expired in the money at {usd(f['price'])} (settled in cash)",
                            "EXERCISED": f"expired in the money at {usd(f['price'])} (settled in cash)"}.get(f["effect"], "expired")
                    ctx.log(f"{ctx.fmt_opt(f['symbol'])} {what}. Realized {usd(f['realized_pnl'] or 0, True)}.",
                            "win" if (f["realized_pnl"] or 0) > 0 else "loss")
                continue
            o = b.get_order(oid) or {}
            parts = [f"{'bought' if f['side'] == 'BUY' else 'sold'} {f['quantity']:g} "
                     f"{ctx.fmt_opt(f['symbol'])} @ {usd(f['price'])}" for f in fs]
            if o.get("order_class") == "MULTILEG":
                nf = o.get("net_fill") or 0
                head = f"Filled {o.get('strategy') or 'multi-leg order'}"
                tail = f" (net {'debit' if nf >= 0 else 'credit'} {usd(abs(nf))}/share)"
            else:
                head = {"LIMIT": "Take-profit hit", "STOP": "Stop-loss hit"}.get(o.get("order_type"), "Filled") \
                    if o.get("order_class") == "BRACKET_EXIT" else "Filled"
                tail = " · bracket armed" if o.get("order_class") == "BRACKET" else ""
            msg = f"{head}: {', '.join(parts)}{tail}."
            if closes:
                msg += f" Realized {usd(pnl, True)}."
            ctx.log(msg, ("win" if pnl > 0 else "loss") if closes else "fill")
        b.ledger.set("agent_last_fill", new[-1]["id"])
        b.ledger.commit()

    # ------------------------------------------------------------ read side
    def _log_rows(self, s: Slot, limit: int) -> list[dict]:
        return s.broker.ledger.rows("SELECT * FROM agent_log ORDER BY id DESC LIMIT ?", (limit,))

    def summary(self, agent_id: str) -> dict:
        with self.lock:
            s = self._slot(agent_id)
            b = s.broker
            acct = b.account()
            closes = b.ledger.rows("SELECT realized_pnl FROM fills WHERE effect != 'OPEN'")
            wins = sum(1 for c in closes if (c["realized_pnl"] or 0) > 0)
            last = self._log_rows(s, 1)
            return {**s.agent.describe(), "enabled": s.enabled,
                    "equity": acct["equity"], "cash": acct["cash"], "day_pnl": acct["day_pnl"],
                    "total_pnl": acct["total_pnl"], "buying_power": acct["buying_power"],
                    "starting_cash": float(b.ledger.get("starting_cash")),
                    "open_positions": len(b._pos_map()),
                    "open_orders": len(b.orders(open_only=True, limit=1000)),
                    "closed_trades": len(closes), "wins": wins,
                    "win_rate": round(wins / len(closes) * 100) if closes else None,
                    "last": last[0] if last else None}

    def summaries(self) -> list[dict]:
        return [self.summary(k) for k in self.slots]

    def detail(self, agent_id: str) -> dict:
        with self.lock:
            s = self._slot(agent_id)
            b = s.broker
            orders = b.orders(limit=40)
            for o in orders:
                opt = parse_option(o["symbol"])
                o["description"] = AgentContext.fmt_opt(o["symbol"]) if opt else o["symbol"]
            eq = b.ledger.rows("SELECT ts, equity FROM equity_history ORDER BY ts DESC LIMIT 500")[::-1]
            return {**self.summary(agent_id), "portfolio": pf.portfolio(b),
                    "orders": orders, "log": self._log_rows(s, 150), "equity_curve": eq}

    def feed(self, limit: int = 60) -> list[dict]:
        with self.lock:
            rows = []
            for k, s in self.slots.items():
                for r in self._log_rows(s, limit):
                    rows.append({**r, "agent": k, "agent_name": s.agent.name})
        rows.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
        return rows[:limit]
