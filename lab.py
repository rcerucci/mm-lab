#!/usr/bin/env python3
"""mm-lab: integrated READ-ONLY observer + perp MM simulator."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import websockets

from config import DATA, DASH_PATH, HEARTBEAT_PATH, HL_COINS, HL_WS, LOG_PATH, PID_PATH
from observer import Observer
from simulator import Simulator
from storage import Store, _rotate


def log(msg: str) -> None:
    ts = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts} America/Sao_Paulo] {msg}"
    print(line, flush=True)
    _rotate(LOG_PATH)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def _exch_time(msg, fallback: float) -> float:
    """HL book and trade `time` are epoch milliseconds; fall back to local now."""
    if not isinstance(msg, dict):
        return fallback
    raw = msg.get("time")
    if raw is None:
        return fallback
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return fallback
    if val > 1e12:
        val /= 1000.0
    return val if val > 0 else fallback


class Lab:
    def __init__(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        self.store = Store()
        self.obs = Observer(self.store)
        self.sim = Simulator(self.store)
        self._stop = asyncio.Event()
        self.started_at = time.time()

    def stop(self) -> None:
        self._stop.set()
        self.obs.stop()

    async def _hl_feed_for_sim(self) -> None:
        backoff = 1.0
        ever_connected = False
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    HL_WS, ping_interval=20, ping_timeout=20, max_size=8_000_000
                ) as ws:
                    now = time.time()
                    if ever_connected:
                        self.sim.on_ws_reconnect(now)
                        log("sim HL WS reconnected; quotes retired")
                    else:
                        log("sim HL WS connected")
                    ever_connected = True
                    backoff = 1.0
                    for coin in HL_COINS:
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "l2Book", "coin": coin},
                        }))
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "trades", "coin": coin},
                        }))
                        await ws.send(json.dumps({
                            "method": "subscribe",
                            "subscription": {"type": "activeAssetCtx", "coin": coin},
                        }))
                    last_save = time.time()
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            now = time.time()
                            self.sim.maybe_refresh(now)
                            self.sim._resolve_as(now)
                            self.sim.apply_funding(now)
                            self.sim.maybe_recalibrate(now)
                            if now - last_save > 10:
                                self.sim.save()
                                self._write_dash()
                                HEARTBEAT_PATH.write_text(str(now))
                                last_save = now
                            continue
                        msg = json.loads(raw)
                        ch = msg.get("channel")
                        data = msg.get("data")
                        now = time.time()
                        if ch == "l2Book" and isinstance(data, dict):
                            coin = data.get("coin")
                            if coin not in HL_COINS:
                                continue
                            levels = data.get("levels") or []
                            try:
                                bids = [(float(x["px"]), float(x["sz"])) for x in levels[0]]
                                asks = [(float(x["px"]), float(x["sz"])) for x in levels[1]]
                            except Exception:
                                continue
                            self.sim.on_book(coin, bids, asks, _exch_time(data, now))
                        elif ch == "activeAssetCtx" and isinstance(data, dict):
                            coin = data.get("coin")
                            ctx = data.get("ctx") or {}
                            if coin in HL_COINS and "funding" in ctx:
                                try:
                                    self.sim.set_funding(coin, float(ctx["funding"]))
                                except Exception:
                                    pass
                        elif ch == "trades" and isinstance(data, list):
                            for tr in data:
                                coin = tr.get("coin")
                                if coin not in HL_COINS:
                                    continue
                                try:
                                    px = float(tr["px"]); sz = float(tr["sz"]); side = tr["side"]
                                except Exception:
                                    continue
                                tid = tr.get("tid")
                                if tid is None:
                                    tid = (tr.get("hash"), px, sz, tr.get("time"))
                                if not self.sim.note_tid(tid):
                                    continue
                                self.sim.on_trade(coin, px, sz, side, _exch_time(tr, now))
                        if now - last_save > 10:
                            self.sim.save()
                            self._write_dash()
                            HEARTBEAT_PATH.write_text(str(now))
                            last_save = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"sim WS error: {e}; reconnect in {backoff:.0f}s")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    def _write_dash(self) -> None:
        """Compact snapshot for the TUI (avoids heavy reads)."""
        try:
            rss_kb = 0
            try:
                with open(f"/proc/{os.getpid()}/status") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            rss_kb = int(line.split()[1])
                            break
            except Exception:
                pass
            obs = self.obs.status_dict()
            payload = {
                "t": time.time(),
                "pid": os.getpid(),
                "rss_mb": rss_kb / 1024.0,
                "started_at": self.sim.started_at,
                "mids": self.sim.mids,
                "imbalance": self.sim.imbalance,
                "funding_rate": self.sim.funding_rate,
                "n_trades": self.sim.n_trades,
                "n_books": self.sim.n_books,
                "observer": obs,
            }
            tmp = DASH_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(DASH_PATH)
        except Exception as e:
            log(f"dash write failed: {e}")

    async def _status_heartbeat(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            HEARTBEAT_PATH.write_text(str(now))
            # pull funding from observer into sim
            for coin, rate in self.obs.last_hl_funding.items():
                self.sim.set_funding(coin, rate)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        log(f"mm-lab starting coins={HL_COINS} arms={list(self.sim.books)} read-only")
        PID_PATH.write_text(str(os.getpid()))
        tasks = [
            asyncio.create_task(self.obs.run(), name="observer"),
            asyncio.create_task(self._hl_feed_for_sim(), name="sim_feed"),
            asyncio.create_task(self._status_heartbeat(), name="hb"),
        ]
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.sim.save()
            self._write_dash()
            self.store.close()
            log("mm-lab stopped")


async def _main() -> None:
    lab = Lab()

    def _handle(*_a: Any) -> None:
        log("signal received, shutting down")
        lab.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle)
        except NotImplementedError:
            pass
    await lab.run()


if __name__ == "__main__":
    asyncio.run(_main())
