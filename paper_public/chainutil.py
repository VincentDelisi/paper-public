"""Option chain enrichment shared by the web app and the agents:
mid price, IV and Greeks per contract, grouped by strike."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .greeks import greeks

ET = ZoneInfo("America/New_York")
RISK_FREE = 0.04


def years_to_expiry(exp: dt.date, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(ET)
    close = dt.datetime.combine(exp, dt.time(16, 0), tzinfo=ET)
    return max((close - now).total_seconds(), 60.0) / (365.0 * 86400.0)


def enrich(rows: list[dict], spot: float | None, expiration: str,
           now: dt.datetime | None = None) -> list[dict]:
    """rows: data.chain() output. Returns [{strike, call: {...}, put: {...}}]
    sorted by strike, each leg with mid/iv/delta/gamma/theta/vega."""
    exp = dt.date.fromisoformat(expiration)
    T = years_to_expiry(exp, now)
    by_strike: dict[float, dict] = {}
    for r in rows:
        mid = (r["bid"] + r["ask"]) / 2 if r["bid"] > 0 and r["ask"] > 0 else None
        g = None
        if mid and spot:
            intrinsic = max(spot - r["strike"], 0) if r["right"] == "C" else max(r["strike"] - spot, 0)
            # Deep ITM: if the time value is smaller than half the spread, the
            # quote can't pin down volatility, so IV/greeks would be noise.
            if mid - intrinsic > (r["ask"] - r["bid"]) / 2:
                g = greeks(mid, spot, r["strike"], T, RISK_FREE, r["right"])
        leg = {**r, "mid": round(mid, 3) if mid else None,
               "iv": round(g.iv, 4) if g and g.iv else None,
               "delta": round(g.delta, 3) if g and g.delta is not None else None,
               "gamma": round(g.gamma, 4) if g and g.gamma is not None else None,
               "theta": round(g.theta, 3) if g and g.theta is not None else None,
               "vega": round(g.vega, 3) if g and g.vega is not None else None}
        by_strike.setdefault(r["strike"], {"strike": r["strike"]})["call" if r["right"] == "C" else "put"] = leg
    return sorted(by_strike.values(), key=lambda x: x["strike"])
