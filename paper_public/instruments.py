"""Instrument helpers. Options use the OCC/OSI symbol, same as Public:

    QQQ261002C00740000  ->  QQQ, 2026-10-02, CALL, strike 740.00
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

_OSI = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")
OPTION_MULTIPLIER = 100


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiration: dt.date
    right: str      # "C" or "P"
    strike: float

    @property
    def symbol(self) -> str:
        return (f"{self.underlying}{self.expiration:%y%m%d}{self.right}"
                f"{int(round(self.strike * 1000)):08d}")


def parse_option(symbol: str) -> OptionContract | None:
    m = _OSI.match(symbol.upper().replace(" ", ""))
    if not m:
        return None
    root, ymd, right, strike = m.groups()
    exp = dt.datetime.strptime(ymd, "%y%m%d").date()
    return OptionContract(root, exp, right, int(strike) / 1000.0)


def is_option(symbol: str) -> bool:
    return parse_option(symbol) is not None


def multiplier(symbol: str) -> int:
    return OPTION_MULTIPLIER if is_option(symbol) else 1


def option_symbol(underlying: str, expiration: dt.date, right: str, strike: float) -> str:
    return OptionContract(underlying.upper(), expiration, right.upper()[0], strike).symbol
