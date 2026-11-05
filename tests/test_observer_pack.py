"""Offline tests for observer helpers changed in the clean-restart pack."""
import math
import time

import pytest

from observer import (
    MakerTracker,
    hl_trade_maker,
    is_hl_trigger_order,
    maker_fills_pnl,
    realized_vol_from_returns,
    should_write_maker_hist,
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


def test_userfills_sort_window_fee_and_empty_use():
    now = 1_700_000_000.0
    fills = [
        {"crossed": False, "coin": "BTC", "time": (now - 10) * 1000, "closedPnl": 5, "fee": 1},
        {"crossed": False, "coin": "HYPE", "time": (now - 100) * 1000, "closedPnl": 3, "fee": 0.5},
        {"crossed": False, "coin": "BTC", "time": (now - 90_000) * 1000, "closedPnl": 100, "fee": 0},
        {"crossed": True, "coin": "BTC", "time": now * 1000, "closedPnl": 50, "fee": 0},
        {"crossed": False, "coin": "ETH", "time": now * 1000, "closedPnl": 7, "fee": 0},
    ]
    use, pnl, n, oldest = maker_fills_pnl(fills, now, 86_400.0)
    assert n == 2
    assert len(use) == 2
    assert pnl == pytest.approx((5 - 1) + (3 - 0.5))
    assert oldest == pytest.approx(now - 100)
    stale = [{"crossed": False, "coin": "BTC", "time": (now - 90_000) * 1000,
              "closedPnl": 0, "fee": 0}]
    use, pnl, n, oldest = maker_fills_pnl(stale, now, 86_400.0)
    assert use == [] and n == 0 and oldest is None


def test_ghost_hist_skipped_when_stale():
    now = time.time()
    tr = MakerTracker("hl", "0xabc", "BTC")
    tr.last_seen = now - 301
    assert should_write_maker_hist(tr, now, 300.0) is False
    tr.last_seen = now - 10
    assert should_write_maker_hist(tr, now, 300.0) is True
