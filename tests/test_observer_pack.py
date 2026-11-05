"""Offline tests for observer helpers changed in the clean-restart pack."""
import math
import time

import pytest

from observer import (
    MakerTracker,
    hl_trade_maker,
    is_hl_trigger_order,
    realized_vol_from_returns,
)


def test_hl_trade_maker_sides():
    users = ["0xaaa0000000000000000000000000000000000000",
             "0xbbb0000000000000000000000000000000000000"]
    assert hl_trade_maker("A", users) == users[0]
    assert hl_trade_maker("B", users) == users[1]
    assert hl_trade_maker("A", ["0xonly"]) is None
    assert hl_trade_maker("C", users) is None


def test_trigger_orders_skipped():
    assert is_hl_trigger_order({"isTrigger": True, "coin": "BTC"})
    assert is_hl_trigger_order({"orderType": "Take Profit Market"})
    assert not is_hl_trigger_order({"coin": "BTC", "side": "B", "limitPx": "1", "sz": "1"})


def test_rvol_is_sum_of_squared_returns():
    rets = [0.01] * 5
    assert realized_vol_from_returns(rets) == pytest.approx(math.sqrt(5 * 0.0001))
    assert realized_vol_from_returns([0.01] * 4) is None


def test_lighter_promissor_needs_presence_and_real_cycles():
    tr = MakerTracker("lighter", "1", "BTC")
    tr.first_seen = time.time()
    tr.n_snaps = 20
    for _ in range(5):
        tr.cycle_samples.append(4.0)
        tr.near_bps.append(12.0)
        tr.size_near.append(80.0)
        tr.n_layers.append(2)
        tr.paused_flags.append(0)
    s = tr.summary()
    assert s["viable"]
    assert not s["promissor"]
    tr.first_seen = time.time() - 301
    tr.n_snaps = 80
    s = tr.summary()
    assert s["promissor"]


def test_hl_cycle_censored_by_poll_gap():
    tr = MakerTracker("hl", "0xabc", "BTC")
    tr.cycle_samples.append(15.0)
    tr.obs_gaps.extend([12.0, 12.5, 13.0])
    s = tr.summary()
    assert s["median_cycle_s"] == pytest.approx(15.0)
    assert s["cycle_usable"] is False
    tr.cycle_samples.clear()
    tr.cycle_samples.append(40.0)
    s = tr.summary()
    assert s["cycle_usable"] is True
