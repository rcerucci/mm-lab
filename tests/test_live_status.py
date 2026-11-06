"""Live unix-socket status for the painel (in-memory, file fallback)."""
import json
import socket
import sys
import threading
import time
import types
from pathlib import Path

# painel imports rich at module load; stub it so tests run without the TUI extra.
if "rich" not in sys.modules:
    rich = types.ModuleType("rich")
    box_m = types.ModuleType("rich.box")
    box_m.SIMPLE_HEAD = None
    align_m = types.ModuleType("rich.align")
    align_m.Align = type("Align", (), {})
    console_m = types.ModuleType("rich.console")
    console_m.Console = type("Console", (), {})
    console_m.Group = type("Group", (), {})
    layout_m = types.ModuleType("rich.layout")
    layout_m.Layout = type("Layout", (), {})
    live_m = types.ModuleType("rich.live")
    live_m.Live = type("Live", (), {})
    panel_m = types.ModuleType("rich.panel")
    panel_m.Panel = type("Panel", (), {})
    table_m = types.ModuleType("rich.table")
    table_m.Table = type("Table", (), {})
    text_m = types.ModuleType("rich.text")
    text_m.Text = type("Text", (), {})
    sys.modules["rich"] = rich
    sys.modules["rich.box"] = box_m
    sys.modules["rich.align"] = align_m
    sys.modules["rich.console"] = console_m
    sys.modules["rich.layout"] = layout_m
    sys.modules["rich.live"] = live_m
    sys.modules["rich.panel"] = panel_m
    sys.modules["rich.table"] = table_m
    sys.modules["rich.text"] = text_m

import painel
import simulator as S
from observer import Observer
from simulator import Simulator
from storage import Store


def test_state_dict_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SIM_STATE_PATH", tmp_path / "sim_state.json")
    monkeypatch.setattr(S, "FILLS_PATH", tmp_path / "fills.jsonl")
    monkeypatch.setattr(S, "PNL_HIST_PATH", tmp_path / "pnl_hist.jsonl")
    sm = Simulator(Store(tmp_path / "obs.sqlite"))
    sm.mids = {"BTC": 76191.5, "HYPE": 77.328}
    payload = sm.state_dict()
    assert not (tmp_path / "sim_state.json").exists()
    assert "A_FIX" in payload["books"]
    assert payload["mids"]["BTC"] == 76191.5
    assert payload["books"]["A_FIX"]["equity"] == sm.books["A_FIX"].equity(sm.mids)


def test_live_market_rows_from_memory(tmp_path):
    obs = Observer(Store(tmp_path / "obs.sqlite"))
    now = time.time()
    obs.feat[("hl", "BTC")].on_book(
        now, [(76100.0, 1.0), (76099.0, 1.0)], [(76102.0, 1.0), (76103.0, 1.0)]
    )
    rows = obs.live_market_rows(now)
    assert len(rows) == 1
    assert rows[0]["venue"] == "hl" and rows[0]["coin"] == "BTC"
    assert rows[0]["mid"] == 76101.0
    assert rows[0]["imb"] is not None


def test_load_live_from_socket(tmp_path):
    sock_path = tmp_path / "lab.sock"
    payload = {
        "source": "live",
        "t": 1.0,
        "books": {"A_FIX": {"equity": 99.86, "inv_cap_pct": 0.0}},
        "market": [{
            "venue": "hl", "coin": "BTC", "mid": 76191.5,
            "imb": 0.12, "rvol": 0.0, "vol": 1.0, "funding": 0.0, "t": 1.0,
        }],
        "makers": [],
    }
    ready = threading.Event()

    def serve() -> None:
        if sock_path.exists():
            sock_path.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(1)
        ready.set()
        conn, _ = srv.accept()
        conn.sendall(json.dumps(payload).encode())
        conn.close()
        srv.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    assert ready.wait(2)
    time.sleep(0.05)
    got = painel._load_live(sock_path, timeout=1.0)
    assert got is not None
    assert got["source"] == "live"
    assert got["market"][0]["mid"] == 76191.5
    assert got["books"]["A_FIX"]["equity"] == 99.86
    th.join(2)


def test_load_live_missing_returns_none(tmp_path):
    assert painel._load_live(tmp_path / "missing.sock") is None
