import math

import pytest

from paper_public.greeks import bs_price, greeks, implied_vol


def test_bs_textbook_value():
    # Hull example: S=42, K=40, r=10%, sigma=20%, T=0.5 -> call 4.76, put 0.81
    assert bs_price(42, 40, 0.5, 0.10, 0.20, "C") == pytest.approx(4.76, abs=0.01)
    assert bs_price(42, 40, 0.5, 0.10, 0.20, "P") == pytest.approx(0.81, abs=0.01)


def test_iv_roundtrip_and_greeks_signs():
    p = bs_price(740, 745, 3 / 365, 0.04, 0.18, "C")
    assert implied_vol(p, 740, 745, 3 / 365, 0.04, "C") == pytest.approx(0.18, abs=1e-4)
    g = greeks(p, 740, 745, 3 / 365, 0.04, "C")
    assert 0 < g.delta < 0.5 and g.gamma > 0 and g.theta < 0 and g.vega > 0
    gp = greeks(bs_price(740, 745, 3 / 365, 0.04, 0.18, "P"), 740, 745, 3 / 365, 0.04, "P")
    assert gp.delta == pytest.approx(g.delta - 1, abs=1e-3)   # put-call parity on delta


def test_iv_none_below_intrinsic():
    assert implied_vol(1.0, 750, 740, 0.01, 0.04, "C") is None
