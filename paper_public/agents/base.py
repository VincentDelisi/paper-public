"""Agent base class and the context an agent sees on every tick.

An agent is a small object with a tick(ctx) method. It reads prices through
ctx, decides, places orders on ctx.broker (its own paper book, the same
PaperBroker API the app uses) and explains itself with ctx.log(). Nothing
here knows about the web server.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any, Optional

from ..broker import ET, PaperBroker
from .. import chainutil
from ..instruments import parse_option


class Agent:
    id: str = "agent"
    name: str = "Agent"
    tagline: str = ""
    rules: list[str] = []
    symbols: list[str] = []
    interval: float = 5.0          # seconds between decisions

    def tick(self, ctx: "AgentContext") -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def reset_memory(self) -> None:
        """Forget in-memory state (price windows etc.) after a book reset."""

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "tagline": self.tagline,
                "rules": self.rules, "symbols": self.symbols, "interval": self.interval}


class AgentContext:
    """Everything an agent needs for one tick. State is a JSON dict persisted
    in the agent's own book, so it survives a server restart."""

    def __init__(self, broker: PaperBroker, data, demo: bool, cache: dict) -> None:
        self.broker = broker
        self.data = data
        self.demo = demo
        self.now: dt.datetime = broker.clock()
        self._cache = cache                     # shared per-agent TTL cache (chains, expirations)
        raw = broker.ledger.get("agent_state")
        try:
            self.state: dict[str, Any] = json.loads(raw) if raw else {}
        except ValueError:
            self.state = {}

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        self.broker.ledger.set("agent_state", json.dumps(self.state))
        self.broker.ledger.commit()

    def day_state(self) -> dict:
        """State that resets every trading day (trade counts, today's high)."""
        today = self.now.date().isoformat()
        if self.state.get("day", {}).get("date") != today:
            self.state["day"] = {"date": today}
        return self.state["day"]

    # ------------------------------------------------------------ logging
    def log(self, message: str, kind: str = "info") -> None:
        self.broker.ledger.execute("INSERT INTO agent_log(ts,kind,message) VALUES(?,?,?)",
                                   (self.now.replace(microsecond=0).isoformat(), kind, message))
        self.broker.ledger.commit()

    def log_every(self, key: str, seconds: float, message: str, kind: str = "watch") -> None:
        """Status line at most once per `seconds` (keeps the feed alive
        without flooding it)."""
        marks = self.state.setdefault("_marks", {})
        t = time.time()
        if t - marks.get(key, 0) >= seconds:
            marks[key] = t
            self.log(message, kind)

    def log_once(self, key: str, message: str, kind: str = "info") -> None:
        """Log once per day per key."""
        d = self.day_state().setdefault("_once", [])
        if key not in d:
            d.append(key)
            self.log(message, kind)

    # ------------------------------------------------------------ market
    def market_open(self) -> bool:
        return True if self.demo else self.broker.market_open()

    def in_window(self, start: tuple[int, int], end: tuple[int, int]) -> bool:
        """Time-of-day filter (ET). Demo mode ignores it so the agents can be
        shown off at any hour."""
        if self.demo:
            return True
        hm = (self.now.hour, self.now.minute)
        return start <= hm < end

    def quote(self, symbol: str):
        q = self.data.quotes([symbol]).get(symbol)
        return q if q is not None and q.valid and not getattr(q, "stale", False) else None

    def quotes(self, symbols) -> dict:
        qs = self.data.quotes(list(symbols))
        return {s: q for s, q in qs.items() if q.valid and not getattr(q, "stale", False)}

    def price(self, symbol: str) -> Optional[float]:
        q = self.quote(symbol)
        return (q.last or q.mid) if q else None

    def _ttl(self, key, ttl: float, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        if val:
            self._cache[key] = (time.time(), val)
        return val

    def expirations(self, symbol: str) -> list[str]:
        return self._ttl(("exp", symbol), 3600, lambda: self.data.expirations(symbol)) or []

    def chain(self, symbol: str, expiration: str) -> list[dict]:
        """Enriched chain (mid, IV, delta...) grouped by strike."""
        rows = self._ttl(("chain", symbol, expiration), 30, lambda: self.data.chain(symbol, expiration)) or []
        spot = self.price(symbol)
        return chainutil.enrich(rows, spot, expiration, self.now) if rows and spot else []

    def dte(self, expiration: str) -> int:
        return (dt.date.fromisoformat(expiration) - self.now.date()).days

    # ------------------------------------------------------------ book
    def position(self, symbol: str) -> float:
        return self.broker._position_qty(symbol)

    def positions(self) -> dict[str, dict]:
        return {r["symbol"]: r for r in self.broker.ledger.rows("SELECT * FROM positions WHERE quantity != 0")}

    def option_positions(self, underlying: str, right: Optional[str] = None) -> dict[str, dict]:
        out = {}
        for s, r in self.positions().items():
            o = parse_option(s)
            if o and o.underlying == underlying and (right is None or o.right == right):
                out[s] = r
        return out

    def open_orders(self) -> list[dict]:
        return self.broker.orders(open_only=True, limit=500)

    @staticmethod
    def fmt_opt(symbol: str) -> str:
        o = parse_option(symbol)
        if not o:
            return symbol
        return f"{o.underlying} {o.expiration:%b} {o.expiration.day} ${o.strike:g} {'Call' if o.right == 'C' else 'Put'}"


def now_et() -> dt.datetime:
    return dt.datetime.now(ET)
