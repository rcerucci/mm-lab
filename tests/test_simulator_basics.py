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
    assert b.as_coin_ntl["BTC"]["1s"] == pytest.approx(100)
    assert b.n_mid_before_fallback >= 1
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
    raw["books"]["A_FIX"].setdefault("quotes", {})["BTC"] = [{
        "bid_px": 99.0, "ask_px": 101.0, "bid_sz": 0.5, "ask_sz": 0.5,
        "placed_at": 0.0, "effective_at": 0.0,
    }]
    S.SIM_STATE_PATH.write_text(json.dumps(raw))
    s2 = Simulator(S.Store(tmp_path / "obs2.sqlite"))
    assert s2.books["A_FIX"].n_fills == 1 and len(s2.books["A_FIX"].pending_as) == 1
    q_old = s2.books["A_FIX"].quotes["BTC"][0]
    assert q_old.beyond_visible_bid is False
    assert q_old.beyond_visible_ask is False
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
    assert b.n_pauses.get("HYPE", 0) >= 1
    assert b.n_pauses.get("BTC", 0) == 0


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
    # increasing fill shrinks room below ORDER_USD so HYPE sizes change (15 → 12)
    b.inv["BTC"] = 0.80
    b.inv["HYPE"] = 0.0
    q = _quote(qa=0.0)
    q.bid_sz = 0.08
    q.ask_sz = 0.0
    q.bid_queue_ahead = 0.0
    b.quotes["BTC"] = [q]
    sim._try_fill(b, "BTC", 99.0, 0.08, "A", now=1.0)
    assert b.n_fills == 1
    assert b.inv["BTC"] == pytest.approx(0.88)
    assert b.inv_usd_gross(sim.mids) == pytest.approx(88.0)
    assert b.n_cap_overshoot == over0
    assert b.n_replaces["BTC"] == n_btc + 1
    assert b.n_replaces["HYPE"] == n_hype + 1
    assert b.replace_reasons["BTC"].get("post_fill", 0) >= 1
    assert b.replace_reasons["HYPE"].get("post_fill", 0) >= 1


def test_timer_refresh_replaces_even_if_sizes_unchanged(sim):
    b = _book(sim)
    sim.books_l2["BTC"] = ([(99.9, 1.0)] * 10, [(100.1, 1.0)] * 10)
    sim._place(b, "BTC", now=0.0)
    n0 = b.n_replaces["BTC"]
    q0 = sim._current_quote(b, "BTC")
    sim.maybe_refresh(30.0)
    assert b.n_replaces["BTC"] == n0 + 1
    assert sim._current_quote(b, "BTC") is not q0


def test_sister_skip_if_sizes_unchanged(sim):
    b = _book(sim)
    sim.books_l2["BTC"] = ([(99.9, 1.0)] * 10, [(100.1, 1.0)] * 10)
    sim.books_l2["HYPE"] = ([(9.99, 1.0)] * 10, [(10.01, 1.0)] * 10)
    sim._place(b, "BTC", now=0.0)
    sim._place(b, "HYPE", now=0.0)
    hype_q = sim._current_quote(b, "HYPE")
    qa = hype_q.bid_queue_ahead
    n_hype = b.n_replaces["HYPE"]
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
    assert b.n_replaces["HYPE"] == n_hype
    live = sim._current_quote(b, "HYPE")
    assert live is hype_q
    assert live.bid_queue_ahead == pytest.approx(qa)


def test_overshoot_only_when_gross_rises_past_cap(sim):
    b = _book(sim)
    b.inv["BTC"] = 0.6
    b.inv["HYPE"] = 5.0
    assert b.inv_usd_gross(sim.mids) == pytest.approx(110.0)
    over0 = b.n_cap_overshoot
    fill(sim, "sell", 101, 0.2)
    assert b.inv["BTC"] == pytest.approx(0.4)
    assert b.n_cap_overshoot == over0
    b.inv["BTC"] = 0.9
    b.inv["HYPE"] = 0.5
    assert b.inv_usd_gross(sim.mids) == pytest.approx(95.0)
    fill(sim, "buy", 100, 0.1)
    assert b.inv["BTC"] == pytest.approx(1.0)
    assert b.inv_usd_gross(sim.mids) == pytest.approx(105.0)
    assert b.n_cap_overshoot == over0 + 1


def test_empty_on_purpose_with_expired_pause_does_not_churn(sim):
    b = _book(sim)
    sim.books_l2["BTC"] = ([(99.9, 1.0)] * 10, [(100.1, 1.0)] * 10)
    sim._place(b, "BTC", now=0.0)
    b.inv["HYPE"] = 10.0
    sim._place(b, "BTC", now=1.0)
    assert b.intentionally_flat["BTC"] is True
    n2 = b.n_replaces["BTC"]
    b.paused_until["BTC"] = 2.0
    sim.maybe_refresh(2.5)
    assert b.n_replaces["BTC"] == n2
    assert b.last_refresh["BTC"] == pytest.approx(2.5)
    for t in (2.55, 2.60, 2.80, 3.00):
        sim.maybe_refresh(t)
    assert b.n_replaces["BTC"] == n2
    assert b.last_refresh["BTC"] == pytest.approx(2.5)


def test_fillable_quotes_half_open(sim):
    b = _book(sim)
    old = _quote(eff=0.0)
    old.retire_at = 10.28
    nxt = _quote(eff=10.28)
    b.quotes["BTC"] = [old, nxt]
    assert sim._fillable_quotes(b, "BTC", 10.0) == [old]
    assert sim._fillable_quotes(b, "BTC", 10.28) == [nxt]


def test_mid_at_locf_gap_and_stale_tail(sim):
    sim.mid_hist["BTC"].clear()
    sim.mid_hist["BTC"].append((0.0, 100.0))
    sim.mid_hist["BTC"].append((2.0, 102.0))
    assert sim._mid_at("BTC", 1.0) == pytest.approx(100.0)
    sim.mid_hist["BTC"].clear()
    sim.mid_hist["BTC"].append((0.0, 100.0))
    sim.mid_hist["BTC"].append((5.0, 102.0))
    assert sim._mid_at("BTC", 2.0) is None
    sim.mid_hist["BTC"].clear()
    sim.mid_hist["BTC"].append((0.0, 100.0))
    assert sim._mid_at("BTC", 2.0) == pytest.approx(100.0)
    assert sim._mid_at("BTC", 4.0) is None


def test_as_discards_interior_gap(sim):
    b = _book(sim)
    fill(sim, "buy", 100, 1)
    sim.mid_hist["BTC"].clear()
    sim.mid_hist["BTC"].append((0.0, 100.0))
    sim.mid_hist["BTC"].append((10.0, 102.0))
    sim._resolve_as(10.0)
    assert b.as_discard.get("1s", 0) >= 1
    assert b.as_discard.get("5s", 0) >= 1
    assert b.pending_as[0].done_1s and b.pending_as[0].done_5s
    assert not b.pending_as[0].done_30s


def test_note_tid_and_ws_reconnect(sim):
    b = _book(sim)
    sim.books_l2["BTC"] = ([(99.9, 1.0)] * 10, [(100.1, 1.0)] * 10)
    assert sim.note_tid(None) is True
    tid = ("0xhash", 99.0, 0.1, 123)
    assert sim.note_tid(tid) is True
    assert sim.note_tid(tid) is False
    assert sim.note_tid(("0xhash", 99.0, 0.1, 124)) is True
    sim._place(b, "BTC", now=0.0)
    assert sim._current_quote(b, "BTC") is not None
    sim.on_ws_reconnect(5.0)
    assert sim._ws_reconnects == 1
    q = b.quotes["BTC"][-1]
    assert q.retire_at == pytest.approx(5.28)


def test_quotes_beyond_visible_counted_and_logged(sim):
    b = _book(sim)
    sim.books_l2["BTC"] = ([(99.99, 1.0)] * 10, [(100.01, 1.0)] * 10)
    sim._place(b, "BTC", now=0.0)
    q = sim._current_quote(b, "BTC")
    assert q is not None
    assert b.n_quotes_placed["BTC"] >= 1
    if q.beyond_visible_bid or q.beyond_visible_ask:
        assert b.n_quotes_beyond["BTC"] >= 1
    q.beyond_visible_bid = True
    q.bid_queue_ahead = 0.0
    sim._try_fill(b, "BTC", 98.9, 0.1, "A", now=1.0)
    lines = S.FILLS_PATH.read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["beyond_visible"] is True


def test_rvol_uses_1s_locf_grid():
    from observer import MarketFeatures, realized_vol_from_returns
    feat = MarketFeatures()
    t0 = 1_000.0
    mid = 100.0
    feat._note_mid(t0, mid)
    for i in range(1, 31):
        mid *= 1.001
        feat._note_mid(t0 + i, mid)
        for j in range(5):
            feat._note_mid(t0 + i + j * 0.05, mid)
    feat.best_bid, feat.best_ask = mid - 0.01, mid + 0.01
    snap = feat.snapshot(t0 + 30.0)
    expected = realized_vol_from_returns([0.001] * 30)
    assert snap["rvol_1m"] == pytest.approx(expected)
    assert len(feat.mids) == 31


# --- HL lot honesty (szDecimals: BTC 5, HYPE 2) ---

def test_lot_floor_below_one_lot_is_zero():
    assert S.lot_floor("BTC", 0.0000099) == 0.0
    assert S.lot_floor("HYPE", 0.0099) == 0.0
    assert S.lot_floor("BTC", 4e-7) == 0.0


def test_lot_floor_1_7_lots_is_one_lot():
    assert S.lot_floor("BTC", 1.7e-5) == pytest.approx(1e-5)
    assert S.lot_floor("HYPE", 0.017) == pytest.approx(0.01)
    assert S.lot_floor("HYPE", -0.017) == pytest.approx(-0.01)


def test_fill_cannot_credit_sub_lot(sim):
    b = _book(sim)
    q = Quote(bid_px=9.9, ask_px=10.1, bid_sz=0.003, ask_sz=0.0, placed_at=0.0,
              effective_at=0.0, bid_queue_ahead=0.0, ask_queue_ahead=0.0,
              bid_sz_opt=0.003, ask_sz_opt=0.0)  # 0.3 HYPE lots
    b.quotes["HYPE"] = [q]
    sim._try_fill(b, "HYPE", 9.8, 5.0, "A", now=1.0)
    assert b.inv["HYPE"] == 0.0 and b.n_fills == 0
    # partial fill of a lot-aligned quote stays lot-aligned
    q2 = Quote(bid_px=9.9, ask_px=10.1, bid_sz=1.5, ask_sz=0.0, placed_at=0.0,
               effective_at=0.0, bid_queue_ahead=0.0, ask_queue_ahead=0.0,
               bid_sz_opt=1.5, ask_sz_opt=0.0)
    b.quotes["HYPE"] = [q2]
    sim._try_fill_one(b, "HYPE", 9.9, 0.737, "A", 1.0, q2)
    assert b.inv["HYPE"] == pytest.approx(0.73)
    assert q2.bid_sz == pytest.approx(0.77)


def test_placed_quote_sizes_are_lot_aligned(sim):
    b = _book(sim)
    sim.mids = {"BTC": 70000.0, "HYPE": 37.123}
    sim.bbo = {"BTC": (69999.0, 70001.0), "HYPE": (37.12, 37.13)}
    sim._place(b, "HYPE", now=1.0)
    q = [x for x in b.quotes["HYPE"] if x.retire_at is None][-1]
    for v in (q.bid_sz, q.ask_sz):
        assert abs(v * 100 - round(v * 100)) < 1e-9
        assert v == 0 or v * 37.123 >= S.HL_MIN_ORDER_USD * 0.99


def test_load_snaps_btc_dust_to_zero(sim, tmp_path):
    b = _book(sim)
    b.inv["BTC"] = 4e-7
    b.avg_entry["BTC"] = 100.0
    b.inv["HYPE"] = 1.23
    sim.save()
    sm2 = Simulator(S.Store(tmp_path / "obs2.sqlite"))
    b2 = sm2.books["A_FIX"]
    assert b2.inv["BTC"] == 0.0 and b2.avg_entry["BTC"] == 0.0
    assert b2.inv["HYPE"] == pytest.approx(1.23)
