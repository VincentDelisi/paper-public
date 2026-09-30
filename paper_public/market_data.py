"""Market data sources.

PublicMarketData  — live bid/ask from Public's quotes endpoint (needs PUBLIC_API_KEY)
MockMarketData    — prices you set by hand; used for tests and offline replay
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from .instruments import is_option, option_symbol, parse_option

log = logging.getLogger(__name__)


class MarketDataError(RuntimeError):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"HTTP {status} {detail}".strip())
        self.status = status


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float | None = None
    timestamp: dt.datetime | None = None
    previous_close: float | None = None
    stale: bool = False

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


class MarketData(Protocol):
    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]: ...


# Optional methods used by the web app (both data sources implement them):
#   expirations(symbol) -> list[str]            ISO dates
#   chain(symbol, expiration) -> list[dict]     one row per contract:
#       {symbol, right, strike, bid, ask, last, volume, open_interest}
#   bars(symbol, period) -> list[dict]          {t (ISO), o, h, l, c, v}
#       period: DAY (5m bars), WEEK (30m), MONTH (1h), YEAR (daily)


class MockMarketData:
    def __init__(self) -> None:
        self._q: dict[str, Quote] = {}

    def set(self, symbol: str, bid: float, ask: float, last: float | None = None) -> None:
        self._q[symbol.upper()] = Quote(symbol.upper(), bid, ask, last if last is not None else (bid + ask) / 2,
                                        dt.datetime.now(dt.timezone.utc))

    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        out = {}
        for sym in symbols:
            sym = sym.upper()
            if sym in self._q:
                out[sym] = self._q[sym]
            elif self.synthetic:
                q = self._synthetic_quote(sym)
                if q:
                    out[sym] = q
        return out

    # ---- synthetic data for demo mode (no API key needed) ----
    synthetic = False
    BASE = {"QQQ": 741.4, "SPY": 767.2, "AAPL": 258.1, "NVDA": 187.3, "TSLA": 441.0,
            "MSFT": 512.6, "AMZN": 231.4, "META": 742.9, "IWM": 244.8, "AMD": 161.2,
            "SOFI": 16.2, "DIA": 462.5, "TLT": 88.4}

    VOL = {"SOFI": 0.52, "TSLA": 0.55, "NVDA": 0.42, "AMD": 0.48, "META": 0.32, "AMZN": 0.30,
           "AAPL": 0.24, "MSFT": 0.22, "IWM": 0.21, "QQQ": 0.19, "SPY": 0.15, "DIA": 0.14, "TLT": 0.14}

    @classmethod
    def demo(cls) -> "MockMarketData":
        m = cls()
        m.synthetic = True
        return m

    def _spot(self, sym: str) -> float | None:
        if sym in self._q:
            return self._q[sym].mid
        base = self.BASE.get(sym)
        if base is None:
            return None
        # A few overlapping waves (periods ~1 to ~15 minutes, phase-shifted per
        # symbol) so demo prices actually trend, dip and break out.
        import math
        s = time.time()
        ph = (sum(map(ord, sym)) % 97) / 97 * 2 * math.pi
        wave = (0.0045 * math.sin(s / 140 + ph) + 0.0025 * math.sin(s / 41 + 2 * ph)
                + 0.0008 * math.sin(s / 9 + 3 * ph))
        return round(base * (1 + wave), 2)

    def _synthetic_quote(self, sym: str) -> Quote | None:
        opt = parse_option(sym)
        if opt:
            row = next((r for r in self.chain(opt.underlying, opt.expiration.isoformat())
                        if r["symbol"] == sym), None)
            if not row:
                return None
            return Quote(sym, row["bid"], row["ask"], row["last"], dt.datetime.now(dt.timezone.utc))
        spot = self._spot(sym)
        if spot is None:
            return None
        spr = max(0.01, round(spot * 0.0001, 2))
        return Quote(sym, round(spot - spr / 2, 2), round(spot + spr / 2, 2), spot,
                     dt.datetime.now(dt.timezone.utc), self.BASE.get(sym, spot) * 0.994)

    def expirations(self, symbol: str) -> list[str]:
        today = dt.date.today()
        out, d = [], today
        while len(out) < 8:
            if d.weekday() in (0, 2, 4) and (d > today or dt.datetime.now().hour < 16):
                out.append(d.isoformat())
            d += dt.timedelta(days=1)
        # weekly Fridays for the next ~9 weeks
        for i in range(1, 64):
            f = today + dt.timedelta(days=i)
            if f.weekday() == 4 and f.isoformat() not in out:
                out.append(f.isoformat())
        # plus the next three monthlies (third Friday)
        y, m = today.year, today.month
        for _ in range(4):
            first = dt.date(y, m, 1)
            third_fri = first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
            if third_fri > today and third_fri.isoformat() not in out:
                out.append(third_fri.isoformat())
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return sorted(out)

    def chain(self, symbol: str, expiration: str) -> list[dict]:
        from .greeks import bs_price
        import math
        spot = self._spot(symbol.upper())
        if spot is None:
            return []
        exp = dt.date.fromisoformat(expiration)
        T = max((exp - dt.date.today()).days + 0.3, 0.05) / 365.0
        step = 1 if spot < 300 else 5
        atm = round(spot / step) * step
        rows = []
        for k in range(-20, 21):
            K = atm + k * step
            if K <= 0:
                continue
            for right in ("C", "P"):
                m = math.log(K / spot)
                iv = self.VOL.get(symbol.upper(), 0.18) + 0.35 * m * m * 100 * T ** 0.3 - (0.08 * m if right == "P" else 0.04 * m)
                iv = max(0.08, iv)
                px = bs_price(spot, K, T, 0.04, iv, right)
                spr = max(0.01, round(px * 0.02, 2))
                bid = max(0.0, round(px - spr / 2, 2))
                ask = round(px + spr / 2, 2) if px > 0.005 else 0.01
                seed = int(K * 10) + (1 if right == "C" else 2)
                oi = int(5000 * math.exp(-abs(k) / 6) * (1 + (seed % 7) / 10))
                rows.append({"symbol": option_symbol(symbol, exp, right, K), "right": right,
                             "strike": float(K), "bid": bid, "ask": ask, "last": round(px, 2),
                             "volume": int(oi * 0.3), "open_interest": oi})
        return rows

    def bars(self, symbol: str, period: str = "DAY") -> list[dict]:
        import random
        spot = self._spot(symbol.upper())
        if spot is None:
            return []
        step, n = {"DAY": (5, 78), "WEEK": (30, 65), "MONTH": (60, 150), "YEAR": (1440, 252)}.get(period, (5, 78))
        rnd = random.Random(hash((symbol, period)) & 0xFFFF)
        px = [spot]
        for _ in range(n - 1):
            px.append(px[-1] * (1 + rnd.gauss(0, 0.0015 * (step / 5) ** 0.5)))
        px = px[::-1]
        now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        out = []
        for i, c in enumerate(px):
            o = px[i - 1] if i else c
            h, l = max(o, c) * 1.0008, min(o, c) * 0.9992
            t = now - dt.timedelta(minutes=step * (n - 1 - i))
            out.append({"t": t.isoformat(), "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                        "c": round(c, 2), "v": rnd.randint(1_000, 50_000)})
        return out


class PublicMarketData:
    """Live quotes from the Public.com API (read-only market data)."""

    BASE_URL = "https://api.public.com"

    def __init__(self, secret: str | None = None, token_minutes: int = 60, timeout: float = 10.0) -> None:
        import threading
        self.timeout = timeout
        self._gate = threading.RLock()
        self.last_status = 200
        if secret is None:
            try:
                from dotenv import load_dotenv
                load_dotenv()
            except ImportError:
                pass
            secret = os.environ.get("PUBLIC_API_KEY", "")
        if not secret:
            raise RuntimeError("PUBLIC_API_KEY not set (add it to .env)")
        self._secret = secret
        self._token_minutes = token_minutes
        self._token: str | None = None
        self._expires = 0.0
        self._account: str | None = None

    # --- plumbing
    def _http(self, method: str, path: str, body: dict | None = None,
              auth: bool = True) -> tuple[int, dict | str]:
        """Single attempt. Retries/back-off live in CachedMarketData so a slow
        API never gets hit harder when it's already struggling."""
        with self._gate:                       # one request in flight at a time
            status, r = self._http_once(method, path, body, auth)
        self.last_status = status
        return status, r

    def _http_once(self, method: str, path: str, body: dict | None = None,
                   auth: bool = True) -> tuple[int, dict | str]:
        headers = {"Content-Type": "application/json", "User-Agent": "paper-public/0.1"}
        if auth:
            headers["Authorization"] = f"Bearer {self._session()[0]}"
        req = urllib.request.Request(self.BASE_URL + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body else None)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:  # noqa: BLE001
                return e.code, ""
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    def _session(self) -> tuple[str, str]:
        now = time.time()
        if self._token and self._expires > now + 60:
            return self._token, self._account  # type: ignore[return-value]
        status, r = self._http("POST", "/userapiauthservice/personal/access-tokens",
                               {"validityInMinutes": self._token_minutes, "secret": self._secret}, auth=False)
        if status != 200 or not isinstance(r, dict) or not r.get("accessToken"):
            raise RuntimeError(f"Public auth failed (HTTP {status})")
        self._token = r["accessToken"]
        self._expires = now + (self._token_minutes - 2) * 60
        status, r = self._http("GET", "/userapigateway/trading/account")
        acct = None
        if status == 200 and isinstance(r, dict):
            acct = next((a.get("accountId") for a in r.get("accounts", [])
                         if a.get("accountType") == "BROKERAGE"), None)
        if not acct:
            raise RuntimeError("No BROKERAGE account found on Public")
        self._account = acct
        return self._token, acct

    # --- public
    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        syms = [s.upper() for s in symbols]
        out: dict[str, Quote] = {}
        for i in range(0, len(syms), 50):
            batch = syms[i:i + 50]
            _, acct = self._session()
            body = {"instruments": [{"symbol": s, "type": "OPTION" if is_option(s) else "EQUITY"}
                                    for s in batch]}
            status, r = self._http("POST", f"/userapigateway/marketdata/{acct}/quotes", body)
            if status != 200 or not isinstance(r, dict):
                raise MarketDataError(status, str(r)[:200])
            for q in r.get("quotes", []) or []:
                sym = ((q.get("instrument") or {}).get("symbol") or "").upper()
                if not sym or q.get("outcome") not in (None, "SUCCESS"):
                    continue
                try:
                    ts = q.get("lastTimestamp")
                    pc = q.get("previousClose")
                    out[sym] = Quote(
                        sym, float(q.get("bid") or 0), float(q.get("ask") or 0),
                        float(q["last"]) if q.get("last") not in (None, "") else None,
                        dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None,
                        float(pc) if pc not in (None, "") else None,
                    )
                except (TypeError, ValueError):
                    continue
        return out

    def expirations(self, symbol: str) -> list[str]:
        _, acct = self._session()
        status, r = self._http("POST", f"/userapigateway/marketdata/{acct}/option-expirations",
                               {"instrument": {"symbol": symbol.upper(), "type": "EQUITY"}})
        if status != 200 or not isinstance(r, dict):
            log.warning("expirations %s failed: HTTP %s %s", symbol, status, str(r)[:200])
            return []
        return sorted(r.get("expirations", []) or [])

    def chain(self, symbol: str, expiration: str) -> list[dict]:
        _, acct = self._session()
        status, r = self._http("POST", f"/userapigateway/marketdata/{acct}/option-chain",
                               {"instrument": {"symbol": symbol.upper(), "type": "EQUITY"},
                                "expirationDate": expiration})
        if status != 200 or not isinstance(r, dict):
            log.warning("chain %s %s failed: HTTP %s %s", symbol, expiration, status, str(r)[:200])
            return []
        exp = dt.date.fromisoformat(expiration)
        rows = []
        for side, right in (("calls", "C"), ("puts", "P")):
            for c in r.get(side, []) or []:
                try:
                    strike = float((c.get("optionDetails") or {}).get("strikePrice"))
                except (TypeError, ValueError):
                    continue
                sym = ((c.get("instrument") or {}).get("symbol") or "").upper() \
                    or option_symbol(symbol, exp, right, strike)

                def f(key):
                    try:
                        v = c.get(key)
                        return float(v) if v not in (None, "") else None
                    except (TypeError, ValueError):
                        return None
                rows.append({"symbol": sym, "right": right, "strike": strike,
                             "bid": f("bid") or 0.0, "ask": f("ask") or 0.0, "last": f("last"),
                             "volume": int(f("volume") or 0), "open_interest": int(f("openInterest") or 0)})
        return rows

    def bars(self, symbol: str, period: str = "DAY") -> list[dict]:
        self._session()
        status, r = self._http("GET", f"/userapigateway/historicdata/EQUITY/{symbol.upper()}/{period}"
                                      "?tradingSessionToggle=REGULAR_HOURS")
        if status != 200 or not isinstance(r, dict):
            log.warning("bars %s %s failed: HTTP %s", symbol, period, status)
            return []
        out = []
        for b in (r.get("regularMarket") or {}).get("bars", []) or []:
            try:
                out.append({"t": b["timestamp"], "o": float(b["open"]), "h": float(b["high"]),
                            "l": float(b["low"]), "c": float(b["close"]), "v": float(b.get("volume") or 0)})
            except (KeyError, TypeError, ValueError):
                continue
        return out


class CachedMarketData:
    """Sits between the app and the API so the app is a polite client.

    * Batching: every quote refresh fetches ALL symbols the app has asked for
      in the last 30s in ONE request (chart symbol, ticket, positions, orders).
    * Freshness: quotes are reused for `ttl` seconds.
    * Back-off: after a timeout or HTTP 429 the API is left alone for 5s, then
      10, 20, 40, up to 60s, while the last good prices keep being served
      (flagged stale). One success resets it.
    """

    def __init__(self, inner, ttl: float = 3.0, stale_for: float = 300.0) -> None:
        import threading
        self.inner, self.ttl, self.stale_for = inner, ttl, stale_for
        self._q: dict[str, tuple[float, Quote]] = {}
        self._wanted: dict[str, float] = {}
        self._lock = threading.Lock()
        self._fail_streak = 0
        self._resume_at = 0.0

    # --- back-off bookkeeping
    @property
    def backing_off(self) -> bool:
        return time.time() < self._resume_at

    def _failed(self, why: str) -> None:
        self._fail_streak += 1
        wait = min(60.0, 5.0 * 2 ** (self._fail_streak - 1))
        self._resume_at = time.time() + wait
        log.warning("market data: %s; backing off %.0fs (serving last good data)", why, wait)

    def _ok(self) -> None:
        if self._fail_streak:
            log.info("market data: recovered")
        self._fail_streak = 0
        self._resume_at = 0.0

    def status(self) -> dict:
        return {"backing_off": self.backing_off, "fail_streak": self._fail_streak,
                "resume_in": max(0.0, round(self._resume_at - time.time(), 1))}

    # --- quotes
    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        now = time.time()
        syms = sorted({s.upper() for s in symbols})
        for s in syms:
            self._wanted[s] = now
        need = [s for s in syms if s not in self._q or now - self._q[s][0] > self.ttl]
        if need and not self.backing_off and self._lock.acquire(blocking=False):
            try:
                batch = sorted({s for s, t in self._wanted.items() if now - t < 30} | set(need))
                try:
                    fresh = self.inner.quotes(batch)
                    self._ok()
                except Exception as e:  # noqa: BLE001
                    fresh = {}
                    self._failed(f"quotes {e}")
                t = time.time()
                for s, q in fresh.items():
                    self._q[s] = (t, q)
            finally:
                self._lock.release()
        out = {}
        now = time.time()
        for s in syms:
            hit = self._q.get(s)
            if hit and now - hit[0] <= self.stale_for:
                q = hit[1]
                q.stale = now - hit[0] > self.ttl * 3
                out[s] = q
        return out

    # --- chain / bars / expirations: skip the call entirely while backing off
    def _guarded(self, name: str, *args):
        if self.backing_off:
            return None
        try:
            val = getattr(self.inner, name)(*args)
        except Exception as e:  # noqa: BLE001
            self._failed(f"{name} {e}")
            return None
        status = getattr(self.inner, "last_status", 200)
        if status in (0, 429) or status >= 500:
            self._failed(f"{name} HTTP {status}")
            return None
        self._ok()
        return val

    def chain(self, symbol: str, expiration: str):
        return self._guarded("chain", symbol, expiration)

    def bars(self, symbol: str, period: str = "DAY"):
        return self._guarded("bars", symbol, period)

    def expirations(self, symbol: str):
        return self._guarded("expirations", symbol)
