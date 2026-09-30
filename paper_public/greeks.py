"""Black-Scholes implied volatility and Greeks (European, no dividends).

Used to show IV / delta / gamma / theta / vega on the options chain from the
live bid/ask. American-style equity options are close enough for display.

Units (what traders expect to see):
  delta  per $1 move in the underlying
  gamma  change in delta per $1 move
  theta  $ per share per CALENDAR day (negative for long options)
  vega   $ per share per 1 vol point (1%)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2 = math.sqrt(2.0)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if right == "C":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float, r: float, right: str) -> float | None:
    """Bisection on sigma in [0.1%, 500%]. None if price is outside no-arb bounds."""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(S - K * math.exp(-r * T), 0.0) if right == "C" else max(K * math.exp(-r * T) - S, 0.0)
    upper = S if right == "C" else K * math.exp(-r * T)
    if price < intrinsic - 1e-9 or price >= upper:
        return None
    lo, hi = 0.001, 5.0
    if bs_price(S, K, T, r, hi, right) < price:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, r, mid, right) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-6:
            break
    return 0.5 * (lo + hi)


@dataclass
class Greeks:
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None


def greeks(price: float, S: float, K: float, T: float, r: float, right: str) -> Greeks:
    iv = implied_vol(price, S, K, T, r, right)
    if iv is None:
        return Greeks(None, None, None, None, None)
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sq)
    d2 = d1 - iv * sq
    pdf = _npdf(d1)
    gamma = pdf / (S * iv * sq)
    vega = S * pdf * sq / 100.0
    if right == "C":
        delta = _ncdf(d1)
        theta = (-S * pdf * iv / (2 * sq) - r * K * math.exp(-r * T) * _ncdf(d2)) / 365.0
    else:
        delta = _ncdf(d1) - 1.0
        theta = (-S * pdf * iv / (2 * sq) + r * K * math.exp(-r * T) * _ncdf(-d2)) / 365.0
    return Greeks(iv, delta, gamma, theta, vega)
