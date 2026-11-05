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
    b.quotes["BTC"] = _quote(qa=1.0)
    sim._try_fill(b, "BTC", 99.0, 0.6, "A", now=1.0)
    assert b.n_fills == 0
    assert b.quotes["BTC"].bid_queue_ahead == pytest.approx(0.4)
    sim._try_fill(b, "BTC", 99.0, 0.6, "A", now=2.0)
    assert b.n_fills == 1 and b.inv["BTC"] == pytest.approx(0.2)


def test_effective_at_gate(sim):
    b = _book(sim)
    b.quotes["BTC"] = _quote(eff=10.0)
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
