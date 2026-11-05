"""Offline tests for simulator accounting, funding, queue and latency gate."""
import json

import pytest

import simulator as S
from simulator import Quote, Simulator


@pytest.fixture
def sim(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SIM_STATE_PATH", tmp_path / "sim_state.json")
    monkeypatch.setattr(S, "FILLS_PATH", tmp_path / "fills.jsonl")
    monkeypatch.setattr(S, "PNL_HIST_PATH", tmp_path / "pnl_hist.jsonl")
    sm = Simulator(S.Store(tmp_path / "obs.sqlite"))
    sm.mids = {"BTC": 100.0, "HYPE": 10.0}
    sm.bbo = {"BTC": (99.99, 100.01), "HYPE": (9.99, 10.01)}
    return sm


def _book(sim):
    return sim.books["A_FIX"]


def fill(sim, side, px, sz, coin="BTC"):
    sim._apply_fill(_book(sim), coin, side, px, sz, now=0.0)


def test_long_open_close(sim):
    b = _book(sim)
    fill(sim, "buy", 100, 1)
    assert b.avg_entry["BTC"] == 100
    fill(sim, "buy", 110, 1)
    assert b.avg_entry["BTC"] == pytest.approx(105)
    fill(sim, "sell", 115, 2)
    assert b.realized_pnl == pytest.approx(20)
    assert b.inv["BTC"] == 0 and b.avg_entry["BTC"] == 0


def test_short_open_add_close(sim):
    b = _book(sim)
    fill(sim, "sell", 100, 1)
    assert b.inv["BTC"] == -1 and b.avg_entry["BTC"] == 100
    fill(sim, "sell", 110, 1)
    assert b.avg_entry["BTC"] == pytest.approx(105)
    fill(sim, "buy", 95, 1)
    assert b.realized_pnl == pytest.approx(10)
    assert b.avg_entry["BTC"] == pytest.approx(105)
    fill(sim, "buy", 100, 1)
    assert b.realized_pnl == pytest.approx(15)
    assert b.inv["BTC"] == 0 and b.avg_entry["BTC"] == 0


def test_flip_long_to_short_and_back(sim):
    b = _book(sim)
    fill(sim, "buy", 100, 1)
    fill(sim, "sell", 110, 3)  # close 1 (+10), short 2 @110
    assert b.realized_pnl == pytest.approx(10)
    assert b.inv["BTC"] == pytest.approx(-2) and b.avg_entry["BTC"] == 110
    fill(sim, "buy", 100, 3)  # cover 2 (+20), long 1 @100
    assert b.realized_pnl == pytest.approx(30)
    assert b.inv["BTC"] == pytest.approx(1) and b.avg_entry["BTC"] == 100


def test_funding_sign_long_positive_rate(sim):
    b = _book(sim)
    b.inv["BTC"] = 2.0
    sim.funding_rate = {"BTC": 0.0001}
    sim.last_funding_apply = 0.0
    sim.apply_funding(S.HL_FUNDING_PERIOD_S)
    assert S.HL_FUNDING_PERIOD_S == 3600
    assert b.funding_pnl == pytest.approx(-2 * 100 * 0.0001)


def _quote(eff=0.0, qa=0.0):
    return Quote(bid_px=99.0, ask_px=101.0, bid_sz=0.5, ask_sz=0.5, placed_at=0.0,
                 effective_at=eff, bid_queue_ahead=qa, ask_queue_ahead=qa,
                 bid_sz_opt=0.5, ask_sz_opt=0.5)


def test_queue_then_fill(sim):
    b = _book(sim)
    b.quotes["BTC"] = [_quote(qa=1.0)]
    sim._try_fill(b, "BTC", 99.0, 0.6, "A", now=1.0)
    assert b.n_fills == 0
    assert b.quotes["BTC"][0].bid_queue_ahead == pytest.approx(0.4)
    sim._try_fill(b, "BTC", 99.0, 0.6, "A", now=2.0)
    assert b.n_fills == 1 and b.inv["BTC"] == pytest.approx(0.2)


def test_effective_at_gate(sim):
    b = _book(sim)
    b.quotes["BTC"] = [_quote(eff=10.0)]
    sim._try_fill(b, "BTC", 98.0, 5.0, "A", now=9.9)
    assert b.n_fills == 0 and b.inv["BTC"] == 0


def test_short_horizon_as(sim):
    b = _book(sim)
    fill(sim, "buy", 100, 1)
    sim.mids["BTC"] = 99.0
    sim._resolve_as(2.0)
    assert b.as_1s_ntl == pytest.approx(100) and b.as_5s_ntl == 0
    assert b.as_1s_sum / b.as_1s_ntl == pytest.approx(0.01)
    sim._resolve_as(301.0)
    assert b.as_5m_ntl == pytest.approx(100) and not b.pending_as


def test_load_old_state_and_corrupt_backup(sim, tmp_path):
    fill(sim, "buy", 100, 1)
    sim.save()
    raw = json.loads(S.SIM_STATE_PATH.read_text())
    for bk in raw["books"].values():  # simulate pre-fix state file
        for k in list(bk):
            if k.startswith(("as_1s", "as_5s", "as_30s", "as_short")):
                del bk[k]
        for p in bk["pending_as"]:
            for k in ("done_1s", "done_5s", "done_30s"):
                p.pop(k, None)
    S.SIM_STATE_PATH.write_text(json.dumps(raw))
    s2 = Simulator(S.Store(tmp_path / "obs2.sqlite"))
    assert s2.books["A_FIX"].n_fills == 1 and len(s2.books["A_FIX"].pending_as) == 1
    pa = s2.books["A_FIX"].pending_as[0]
    assert pa.done_1s and pa.done_5s and pa.done_30s and not pa.done_1m
    assert s2.books["A_FIX"].as_short_since > 0
    S.SIM_STATE_PATH.write_text("{not json")
    s3 = Simulator(S.Store(tmp_path / "obs3.sqlite"))
    assert s3.books["A_FIX"].n_fills == 0
    assert list(tmp_path.glob("sim_state.json.bak-*"))


def test_old_quote_lives_until_retire(sim):
    b = _book(sim)
    old = _quote(eff=0.0)
    old.retire_at = 10.28
    nxt = _quote(eff=10.28)
    nxt.bid_px = 98.0
    b.quotes["BTC"] = [old, nxt]
    # use _try_fill_one so a post-fill requote does not rewrite the injected chain
    sim._try_fill_one(b, "BTC", 99.0, 0.1, "A", now=10.0, q=old)
    assert b.n_fills == 1 and b.inv["BTC"] == pytest.approx(0.1)
    assert old.bid_sz == pytest.approx(0.4)
    assert nxt.bid_sz == pytest.approx(0.5)
    sim._try_fill_one(b, "BTC", 98.0, 0.1, "A", now=10.28, q=nxt)
    assert b.n_fills == 2
    assert nxt.bid_sz == pytest.approx(0.4)
    sim._sweep_expired(b, "BTC", 10.28)
    assert old not in b.quotes["BTC"]


def test_pause_keeps_quote_until_retire(sim):
    b = _book(sim)
    b.quotes["BTC"] = [_quote(eff=0.0)]
    b.paused_until["BTC"] = 1e9
    sim._retire_quote(b, "BTC", now=5.0)
    assert b.quotes["BTC"][0].retire_at == pytest.approx(5.28)
    sim._try_fill(b, "BTC", 99.0, 0.1, "A", now=5.1)
    assert b.n_fills == 1
    sim._try_fill(b, "BTC", 99.0, 0.1, "A", now=5.28)
    assert b.n_fills == 1
    assert b.quotes["BTC"] == []


def test_gross_cap_offsets_do_not_cancel(sim):
    b = _book(sim)
    b.inv["BTC"] = 0.6
    b.inv["HYPE"] = -5.0
    assert b.inv_usd(sim.mids) == pytest.approx(10.0)
    assert b.inv_usd_gross(sim.mids) == pytest.approx(110.0)
    assert sim._would_respect_cap(b, "BTC", "buy", 0.1, 100.0) is False
    assert sim._would_respect_cap(b, "BTC", "sell", 0.1, 100.0) is True


def test_as_discards_late_horizon(sim):
    b = _book(sim)
    fill(sim, "buy", 100, 1)
    sim.mids["BTC"] = 99.0
    sim._resolve_as(10.0)
    assert b.as_1s_ntl == 0 and b.as_5s_ntl == 0
    assert b.pending_as[0].done_1s and b.pending_as[0].done_5s
    assert not b.pending_as[0].done_30s


def test_chained_replace_keeps_middle_quote(sim):
    b = _book(sim)
    a = _quote(eff=0.0)
    a.retire_at = 0.38
    bb = _quote(eff=0.38)
    bb.retire_at = 0.48
    c = _quote(eff=0.48)
    c.bid_px = 97.0
    b.quotes["BTC"] = [a, bb, c]
    sim._try_fill_one(b, "BTC", 99.0, 0.1, "A", now=0.30, q=a)
    assert b.n_fills == 1
    assert a.bid_sz == pytest.approx(0.4)
    assert bb.bid_sz == pytest.approx(0.5)
    assert c.bid_sz == pytest.approx(0.5)
    sim._try_fill_one(b, "BTC", 99.0, 0.1, "A", now=0.40, q=bb)
    assert b.n_fills == 2
    assert bb.bid_sz == pytest.approx(0.4)
    assert c.bid_sz == pytest.approx(0.5)


def test_jump_uses_ref_mid_not_skewed_qmid(sim):
    b = _book(sim)
    b.inv["BTC"] = 0.25
    b.refresh_s = 30.0
    sim.books_l2["BTC"] = (
        [(99.9, 1.0)] * 10,
        [(100.1, 1.0)] * 10,
    )
    sim._place(b, "BTC", now=0.0)
    q = sim._current_quote(b, "BTC")
    assert q is not None and q.ref_mid == pytest.approx(100.0)
    n0 = b.n_replaces["BTC"]
    sim.maybe_refresh(0.1)
    assert b.n_replaces["BTC"] == n0


def test_cap_edge_not_level(sim):
    b = _book(sim)
    b.inv["BTC"] = 0.70
    b.inv["HYPE"] = 3.0
    assert b.inv_usd_gross(sim.mids) == pytest.approx(100.0)
    sim.books_l2["BTC"] = ([(99.9, 1.0)] * 10, [(100.1, 1.0)] * 10)
    sim._place(b, "BTC", now=0.0)
    n0 = b.n_replaces["BTC"]
    sim.maybe_refresh(0.05)
    assert b.n_replaces["BTC"] == n0


def test_pause_is_per_coin(sim):
    b = _book(sim)
    b.quotes["BTC"] = [_quote(eff=0.0)]
    b.quotes["HYPE"] = [_quote(eff=0.0)]
    sim.mids["HYPE"] = 10.0
    q = b.quotes["HYPE"][0]
    q.ref_mid = 10.0
    # 40 bps jump on HYPE only (3 * 10 bps = 30 bps)
    sim.mids["HYPE"] = 10.04
    sim.maybe_refresh(1.0)
    assert b.coin_paused("HYPE", 1.0)
    assert not b.coin_paused("BTC", 1.0)


def test_reducing_residual_is_allowed(sim):
    b = _book(sim)
    b.inv["BTC"] = 0.06
    b.inv["HYPE"] = 9.5
    assert b.inv_usd_gross(sim.mids) == pytest.approx(101.0)
    assert sim._would_respect_cap(b, "BTC", "sell", 0.15, 100.0) is False
    assert sim._would_respect_cap(b, "BTC", "sell", 0.06, 100.0) is True
    assert sim._clamp_reduce(b, "BTC", "sell", 0.15) == pytest.approx(0.06)


def test_trade_through_counts_separately(sim):
    b = _book(sim)
    q = _quote(qa=5.0)
    b.quotes["BTC"] = [q]
    sim._try_fill(b, "BTC", 98.9, 0.2, "A", now=1.0)
    assert b.n_fills == 1
    assert q.bid_sz == pytest.approx(0.0)
    assert b.n_fills_through == 1
    assert b.n_fills_tt == 1
    assert b.n_fills_through <= b.n_fills


def test_touch_high_queue_does_not_fill(sim):
    b = _book(sim)
    q = _quote(qa=5.0)
    b.quotes["BTC"] = [q]
    sim._try_fill(b, "BTC", 99.0, 0.2, "A", now=1.0)
    assert b.n_fills == 0
    assert b.n_fills_through == 0
    assert q.bid_sz == pytest.approx(0.5)
    assert q.bid_queue_ahead == pytest.approx(4.8)


def test_empty_on_purpose_does_not_churn(sim):
    b = _book(sim)
    sim.books_l2["BTC"] = ([(99.9, 1.0)] * 10, [(100.1, 1.0)] * 10)
    sim._place(b, "BTC", now=0.0)
    assert sim._current_quote(b, "BTC") is not None
    n1 = b.n_replaces["BTC"]
    b.inv["HYPE"] = 10.0  # BTC flat, book at the $100 cap
    sim._place(b, "BTC", now=1.0)
    assert b.intentionally_flat["BTC"] is True
    assert b.n_replaces["BTC"] == n1 + 1
    n2 = b.n_replaces["BTC"]
    for t in (1.05, 1.10, 1.20, 1.50):
        sim.maybe_refresh(t)
    assert b.n_replaces["BTC"] == n2


def test_requote_both_coins_after_fill_no_overshoot_on_reduce(sim):
    b = _book(sim)
    sim.books_l2["BTC"] = ([(99.9, 1.0)] * 10, [(100.1, 1.0)] * 10)
    sim.books_l2["HYPE"] = ([(9.99, 1.0)] * 10, [(10.01, 1.0)] * 10)
    sim._place(b, "BTC", now=0.0)
    sim._place(b, "HYPE", now=0.0)
    n_btc = b.n_replaces["BTC"]
    n_hype = b.n_replaces["HYPE"]
    over0 = b.n_cap_overshoot
    b.inv["BTC"] = 0.5
    b.inv["HYPE"] = 2.0
    q = _quote(qa=0.0)
    q.bid_sz = 0.0
    q.ask_sz = 0.2
    q.ask_queue_ahead = 0.0
    b.quotes["BTC"] = [q]
    sim._try_fill(b, "BTC", 101.0, 0.2, "B", now=1.0)
    assert b.n_fills == 1
    assert b.inv["BTC"] == pytest.approx(0.3)
    assert b.n_cap_overshoot == over0
    assert b.n_replaces["BTC"] == n_btc + 1
    assert b.n_replaces["HYPE"] == n_hype + 1
    assert b.replace_reasons["BTC"].get("post_fill", 0) >= 1
    assert b.replace_reasons["HYPE"].get("post_fill", 0) >= 1


def test_rvol_uses_1s_locf_grid():
    from observer import MarketFeatures
    feat = MarketFeatures()
    t0 = 1_000.0
    mid = 100.0
    for i in range(30):
        feat.mids.append((t0 + i * 0.02, mid))
        mid *= 1.001
    feat.best_bid, feat.best_ask = 103.0, 103.02
    snap = feat.snapshot(t0 + 10.0)
    assert snap["rvol_1m"] is not None
    # 1s LOCF compresses the 30 intra-second ticks into a single 1s return
    assert snap["rvol_1m"] < 0.05
