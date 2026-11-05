"""Read-only MM observer: Lighter (owner_account_index) then Hyperliquid."""
from __future__ import annotations

import asyncio
import sqlite3
import json
import math
import statistics
import time
import urllib.request
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import websockets

from config import (
    BOOK_DEPTH_FOR_IMB,
    HL_ADDR_COLD_POLL_S,
    HL_ADDR_HOT_POLL_S,
    HL_COINS,
    HL_HOT_MAKERS,
    HL_INFO,
    HL_WS,
    LIGHTER_MARKETS,
    LIGHTER_ORDERS_POLL_S,
    LIGHTER_REST,
    LIGHTER_WS,
    MARKET_SNAP_S,
    MAKER_SUMMARY_S,
    MAX_TRACKED_HL,
    MAX_TRACKED_LIGHTER,
    ROLLING_WINDOW_S,
    VIABLE_MAX_DIST_BPS,
    VIABLE_MAX_PAUSE_FRAC,
    VIABLE_MIN_CYCLE_S,
    VIABLE_MIN_DIST_BPS,
    VIABLE_MIN_LAYERS,
    VIABLE_MIN_SIZE_USD,
    PROMISSOR_MIN_SNAPS,
    PROMISSOR_MIN_PRESENCE_S,
    PROMISSOR_MIN_CYCLE_SAMPLES,
    HL_CYCLE_CENSOR_MULT,
)
from storage import Store


def hl_trade_maker(side: Optional[str], users) -> Optional[str]:
    """Hyperliquid trade `users` is [buyer, seller].
    Side A = taker sold, so the maker is the buyer (users[0]).
    Side B = taker bought, so the maker is the seller (users[1]).
    """
    if not isinstance(users, list) or len(users) < 2:
        return None
    if side == "B":
        maker = users[1]
    elif side == "A":
        maker = users[0]
    else:
        return None
    if isinstance(maker, str) and maker.startswith("0x"):
        return maker.lower()
    return None


def is_hl_trigger_order(order: Dict[str, Any]) -> bool:
    if not isinstance(order, dict):
        return True
    if order.get("isTrigger"):
        return True
    ot = str(order.get("orderType") or "").lower().replace(" ", "")
    return ot in {
        "trigger", "stop", "stoplimit", "stopmarket",
        "takeprofit", "takeprofitmarket", "takeprofitlimit",
    }


def realized_vol_from_returns(rets: List[float]) -> Optional[float]:
    """1-minute realized vol = sqrt(sum of squared tick returns)."""
    if len(rets) < 5:
        return None
    return math.sqrt(sum(r * r for r in rets))


def _median(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return float(statistics.median(xs))


def _http_json(url: str, payload: Optional[dict] = None, timeout: float = 20.0) -> Any:
    data = None
    headers = {"User-Agent": "mm-lab/0.2 (read-only research)"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _imbalance(bids: List[Tuple[float, float]], asks: List[Tuple[float, float]], n: int) -> float:
    bv = sum(s for _, s in bids[:n])
    av = sum(s for _, s in asks[:n])
    tot = bv + av
    return ((bv - av) / tot) if tot > 0 else 0.0


class MakerTracker:
    def __init__(self, venue: str, maker_id: str, coin: str) -> None:
        self.venue = venue
        self.maker_id = maker_id
        self.coin = coin
        self.first_seen = time.time()
        self.last_seen = self.first_seen
        # (t, frozenset of order_ids)
        self.order_sets: Deque[Tuple[float, frozenset]] = deque(maxlen=240)
        self.near_bps: Deque[float] = deque(maxlen=300)
        self.far_bps: Deque[float] = deque(maxlen=300)
        self.size_near: Deque[float] = deque(maxlen=300)
        self.size_far: Deque[float] = deque(maxlen=300)
        self.n_layers: Deque[int] = deque(maxlen=300)
        self.skews: Deque[float] = deque(maxlen=300)
        self.paused_flags: Deque[int] = deque(maxlen=300)
        self.cycle_samples: Deque[float] = deque(maxlen=200)
        self.obs_gaps: Deque[float] = deque(maxlen=80)
        self._last_obs_t: Optional[float] = None
        self.est_pnl = 0.0
        self.maker_fills = 0
        self.pnl_known = False  # True after HL userFills or Lighter trade pnl field
        self.n_snaps = 0
        self._last_change_t: Optional[float] = None

    def observe_orders(
        self,
        now: float,
        mid: float,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
        order_ids: Optional[frozenset] = None,
    ) -> Dict[str, Any]:
        if self._last_obs_t is not None and now > self._last_obs_t:
            self.obs_gaps.append(now - self._last_obs_t)
        self._last_obs_t = now
        self.last_seen = now
        self.n_snaps += 1
        if order_ids is None:
            order_ids = frozenset(
                [f"B:{p:.8g}:{s:.8g}" for p, s in bids]
                + [f"A:{p:.8g}:{s:.8g}" for p, s in asks]
            )
        if self.order_sets:
            prev_t, prev_ids = self.order_sets[-1]
            if order_ids != prev_ids and now > prev_t:
                # lifetime of previous set
                stable_from = prev_t
                for tt, iid in reversed(list(self.order_sets)[:-1]):
                    if iid == prev_ids:
                        stable_from = tt
                    else:
                        break
                lifetime = now - stable_from  # from first sighting of prev to change
                # With fine poll (~1.5s), lifetime is a usable cycle estimate.
                # If only seen once before change, lifetime ≈ poll gap — still a
                # measured upper-ish bound; record it (not the old 0 sentinel).
                self.cycle_samples.append(max(0.05, lifetime))
                self._last_change_t = now
        else:
            self._last_change_t = now
        self.order_sets.append((now, order_ids))

        paused = 1 if (not bids and not asks) else 0
        self.paused_flags.append(paused)

        def bps(px: float) -> float:
            return abs(px - mid) / mid * 1e4 if mid > 0 else 0.0

        near_b = sorted(bps(p) for p, _ in bids)[:3] if bids else []
        near_a = sorted(bps(p) for p, _ in asks)[:3] if asks else []
        near = near_b + near_a
        far_all = [bps(p) for p, _ in bids + asks]
        if near:
            self.near_bps.append(statistics.median(near))
        if far_all:
            self.far_bps.append(max(far_all))
        if bids:
            self.size_near.append(sum(p * s for p, s in bids[:2]))
        if asks:
            self.size_far.append(sum(p * s for p, s in asks[:2]))
        self.n_layers.append(len(bids) + len(asks))
        if near_b and near_a:
            self.skews.append(statistics.median(near_a) - statistics.median(near_b))

        return {
            "t": now, "venue": self.venue, "coin": self.coin, "maker_id": self.maker_id,
            "n_bid": len(bids), "n_ask": len(asks),
            "near_bps": self.near_bps[-1] if self.near_bps else None,
            "far_bps": self.far_bps[-1] if self.far_bps else None,
            "size_usd_near": self.size_near[-1] if self.size_near else None,
            "size_usd_far": self.size_far[-1] if self.size_far else None,
            "skew": self.skews[-1] if self.skews else None,
            "paused": paused,
        }

    def summary(self) -> Dict[str, Any]:
        samples = [c for c in self.cycle_samples if c is not None]
        cycle = _median(samples) if samples else None
        near = _median(list(self.near_bps))
        size = _median(list(self.size_near) + list(self.size_far))
        layers = _median([float(x) for x in self.n_layers]) if self.n_layers else None
        pause_frac = (
            sum(self.paused_flags) / len(self.paused_flags) if self.paused_flags else 0.0
        )
        viable = (
            cycle is not None
            and near is not None
            and cycle >= VIABLE_MIN_CYCLE_S
            and VIABLE_MIN_DIST_BPS <= near <= VIABLE_MAX_DIST_BPS
            and (size is None or size >= VIABLE_MIN_SIZE_USD)
            and (layers is None or layers >= VIABLE_MIN_LAYERS)
            and pause_frac <= VIABLE_MAX_PAUSE_FRAC
        )
        n_cycles = len(samples)
        stable = n_cycles >= PROMISSOR_MIN_CYCLE_SAMPLES
        presence = (
            (time.time() - self.first_seen) >= PROMISSOR_MIN_PRESENCE_S
            and self.n_snaps >= PROMISSOR_MIN_SNAPS
        )
        poll_gap = _median(list(self.obs_gaps)) if self.obs_gaps else None
        cycle_usable = True
        if self.venue == "hl":
            if cycle is None:
                cycle_usable = False
            elif poll_gap is not None and poll_gap > 0:
                cycle_usable = cycle >= HL_CYCLE_CENSOR_MULT * poll_gap
            else:
                cycle_usable = False
        if self.pnl_known:
            # known PnL: only winners feed recalibration
            promissor = bool(viable and self.est_pnl > 0)
        else:
            # unknown PnL (typical Lighter): presence + stable cycle, no fill OR
            promissor = bool(viable and stable and presence)
        return {
            "venue": self.venue, "maker_id": self.maker_id, "coin": self.coin,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "n_snaps": self.n_snaps, "median_cycle_s": cycle,
            "cycle_usable": cycle_usable, "poll_gap_s": poll_gap,
            "median_near_bps": near, "median_size_usd": size,
            "median_layers": layers, "pause_frac": pause_frac,
            "est_pnl": self.est_pnl, "maker_fills": self.maker_fills,
            "viable": viable, "promissor": promissor, "pnl_known": self.pnl_known,
        }


class MarketFeatures:
    def __init__(self) -> None:
        self.mids: Deque[Tuple[float, float]] = deque(maxlen=600)
        self.trades_ntl: Deque[Tuple[float, float]] = deque(maxlen=2000)
        self.funding: Optional[float] = None
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.bid_levels: List[Tuple[float, float]] = []
        self.ask_levels: List[Tuple[float, float]] = []
        self.imbalance: float = 0.0

    def on_book(self, t: float, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]) -> None:
        if not bids or not asks:
            return
        self.bid_levels = bids
        self.ask_levels = asks
        self.best_bid, self.best_ask = bids[0][0], asks[0][0]
        mid = (self.best_bid + self.best_ask) / 2.0
        self.mids.append((t, mid))
        self.imbalance = _imbalance(bids, asks, BOOK_DEPTH_FOR_IMB)

    def on_bbo(self, t: float, bid: float, ask: float) -> None:
        self.best_bid, self.best_ask = bid, ask
        self.mids.append((t, (bid + ask) / 2.0))

    def on_trade(self, t: float, ntl: float) -> None:
        self.trades_ntl.append((t, ntl))

    def snapshot(self, t: float) -> Dict[str, Any]:
        mid = None
        if self.best_bid and self.best_ask:
            mid = (self.best_bid + self.best_ask) / 2.0
        cutoff = t - 60.0
        recent = [(tt, m) for tt, m in self.mids if tt >= cutoff]
        rvol = None
        spike = False
        if len(recent) >= 5:
            rets = []
            for i in range(1, len(recent)):
                a, b = recent[i - 1][1], recent[i][1]
                if a > 0:
                    rets.append((b - a) / a)
            if rets:
                rvol = realized_vol_from_returns(rets)
                if abs(rets[-1]) > 0.001:
                    spike = True
        vol_1m = sum(n for tt, n in self.trades_ntl if tt >= cutoff)
        imb = self.imbalance
        if self.bid_levels and self.ask_levels:
            imb = _imbalance(self.bid_levels, self.ask_levels, BOOK_DEPTH_FOR_IMB)
        return {
            "t": t, "mid": mid, "best_bid": self.best_bid, "best_ask": self.best_ask,
            "imbalance": imb, "rvol_1m": rvol, "volume_1m": vol_1m,
            "funding": self.funding, "spike": spike,
        }


class Observer:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.lighter_makers: Dict[Tuple[str, str], MakerTracker] = {}
        self.hl_makers: Dict[Tuple[str, str], MakerTracker] = {}
        # account-level HL PnL from userFills: addr -> (closedPnl_sum, maker_fills)
        self.hl_acct_pnl: Dict[str, Tuple[float, int]] = {}
        self.hl_addrs: Dict[str, float] = {}
        self.hl_addr_score: Dict[str, float] = defaultdict(float)  # activity score
        self.feat: Dict[Tuple[str, str], MarketFeatures] = defaultdict(MarketFeatures)
        self.n_lighter_polls = 0
        self.n_hl_addr_polls = 0
        self.n_lighter_orders = 0
        self.n_hl_trades = 0
        # (t, tag) — TTL so health line clears after recovery
        self.api_errors: Deque[Tuple[float, str]] = deque(maxlen=40)
        self._err_ttl_s = 120.0
        self._stop = asyncio.Event()
        self.last_hl_funding: Dict[str, float] = {}
        self.last_funding_rest_t = 0.0
        self._hl_poll_cursor = 0
        self._hl_fills_tick = 0

    def stop(self) -> None:
        self._stop.set()

    def _get_lighter_tracker(self, coin: str, owner: str) -> MakerTracker:
        key = (coin, str(owner))
        if key not in self.lighter_makers:
            if len(self.lighter_makers) >= MAX_TRACKED_LIGHTER:
                oldest = min(self.lighter_makers.values(), key=lambda m: m.last_seen)
                del self.lighter_makers[(oldest.coin, oldest.maker_id)]
            self.lighter_makers[key] = MakerTracker("lighter", str(owner), coin)
        return self.lighter_makers[key]

    def _get_hl_tracker(self, coin: str, addr: str) -> MakerTracker:
        key = (coin, addr.lower())
        if key not in self.hl_makers:
            if len(self.hl_makers) >= MAX_TRACKED_HL:
                oldest = min(self.hl_makers.values(), key=lambda m: m.last_seen)
                del self.hl_makers[(oldest.coin, oldest.maker_id)]
            tr = MakerTracker("hl", addr.lower(), coin)
            acct = self.hl_acct_pnl.get(addr.lower())
            if acct is not None:  # inherit account-level PnL
                tr.pnl_known = True
                tr.est_pnl, tr.maker_fills = acct
            self.hl_makers[key] = tr
        return self.hl_makers[key]

    def _seed_from_db(self) -> None:
        """Restore fill/PnL counters so a soft restart does not zero promissor state."""
        try:
            rows = self.store.recent_maker_seed(time.time() - 86400)
        except Exception:
            return
        for row in rows:
            venue, mid, coin, pnl, fills, pk, snaps, med_cycle = row
            if venue == "lighter":
                tr = self._get_lighter_tracker(coin, str(mid))
            elif venue == "hl":
                tr = self._get_hl_tracker(coin, str(mid))
            else:
                continue
            tr.maker_fills = max(tr.maker_fills, int(fills or 0))
            if pk:
                tr.pnl_known = True
                tr.est_pnl = float(pnl or 0)
            elif pnl and float(pnl) != 0:
                tr.est_pnl = float(pnl)
            # Do not restore n_snaps or fabricate cycle samples: presence and
            # cycle stability must be earned live after a restart.

    async def run(self) -> None:
        self._seed_from_db()
        await asyncio.gather(
            self._lighter_orders_loop(),
            self._lighter_ws_loop(),
            self._hl_ws_loop(),
            self._hl_addr_loop(),
            self._market_snap_loop(),
            self._summary_loop(),
            self._prune_loop(),
        )

    def _note_err(self, tag: str, exc: BaseException) -> None:
        """Record a short error for the painel; classify sqlite vs HTTP."""
        if isinstance(exc, sqlite3.Error):
            msg = f"{tag}:sqlite:{exc}"
        else:
            msg = f"{tag}:{exc}"
        # keep message short for the health line
        if len(msg) > 90:
            msg = msg[:87] + "..."
        self.api_errors.append((time.time(), msg))

    def _recent_errs(self) -> List[str]:
        now = time.time()
        return [m for ts, m in self.api_errors if now - ts <= self._err_ttl_s][-5:]

    async def _lighter_orders_loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                await asyncio.to_thread(self._poll_lighter_orders)
            except Exception as e:
                self._note_err("lighter_orders", e)
            elapsed = time.time() - t0
            wait = max(0.2, LIGHTER_ORDERS_POLL_S - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    def _poll_lighter_orders(self) -> None:
        now = time.time()
        for coin, mid_id in LIGHTER_MARKETS.items():
            url = f"{LIGHTER_REST}/api/v1/orderBookOrders?market_id={mid_id}&limit=150"
            try:
                raw = _http_json(url, timeout=12)
            except Exception as e:
                self._note_err(f"lighter_{coin}", e)
                continue
            self.n_lighter_polls += 1
            asks_raw = raw.get("asks") or []
            bids_raw = raw.get("bids") or []
            self.n_lighter_orders += len(asks_raw) + len(bids_raw)
            bids = [(float(o["price"]), float(o["remaining_base_amount"])) for o in bids_raw]
            asks = [(float(o["price"]), float(o["remaining_base_amount"])) for o in asks_raw]
            bids.sort(key=lambda x: -x[0])
            asks.sort(key=lambda x: x[0])
            if not bids or not asks:
                continue
            mid = (bids[0][0] + asks[0][0]) / 2.0
            feat = self.feat[("lighter", coin)]
            feat.on_book(now, bids, asks)

            by_owner: Dict[str, Dict[str, Any]] = defaultdict(
                lambda: {"bids": [], "asks": [], "oids": set()}
            )
            for o in bids_raw:
                owner = str(o.get("owner_account_index"))
                by_owner[owner]["bids"].append(
                    (float(o["price"]), float(o["remaining_base_amount"]))
                )
                by_owner[owner]["oids"].add(f"B:{o.get('order_index') or o.get('order_id')}")
            for o in asks_raw:
                owner = str(o.get("owner_account_index"))
                by_owner[owner]["asks"].append(
                    (float(o["price"]), float(o["remaining_base_amount"]))
                )
                by_owner[owner]["oids"].add(f"A:{o.get('order_index') or o.get('order_id')}")
            ranked = sorted(
                by_owner.items(),
                key=lambda kv: sum(p * s for p, s in kv[1]["bids"] + kv[1]["asks"]),
                reverse=True,
            )[:30]
            for owner, sides in ranked:
                tr = self._get_lighter_tracker(coin, owner)
                snap = tr.observe_orders(
                    now, mid, sides["bids"], sides["asks"], frozenset(sides["oids"])
                )
                try:
                    self.store.insert_maker_snap(snap)
                except sqlite3.Error as e:
                    self._note_err("lighter_db", e)
                    # keep in-memory maker state; skip this snap row
            try:
                self.store.commit()
            except sqlite3.Error as e:
                self._note_err("lighter_db", e)

    async def _lighter_ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    LIGHTER_WS, ping_interval=60, ping_timeout=60, max_size=4_000_000
                ) as ws:
                    backoff = 1.0
                    for coin, mid in LIGHTER_MARKETS.items():
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"trade/{mid}"}))
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{mid}"}))
                        await ws.send(json.dumps({"type": "subscribe", "channel": f"market_stats/{mid}"}))
                    # local aggregated books for imbalance (WS has no owner)
                    local_books: Dict[int, Dict[str, Dict[str, float]]] = {
                        m: {"bids": {}, "asks": {}} for m in LIGHTER_MARKETS.values()
                    }
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        msg = json.loads(raw)
                        typ = msg.get("type") or ""
                        now = time.time()
                        ch = str(msg.get("channel", ""))
                        mid = None
                        if ":" in ch:
                            try:
                                mid = int(ch.split(":")[-1].split("/")[-1])
                            except Exception:
                                mid = None
                        coin = next((c for c, m in LIGHTER_MARKETS.items() if m == mid), None)

                        if "order_book" in typ and coin is not None and mid is not None:
                            ob = msg.get("order_book") or {}
                            book = local_books[mid]
                            # snapshot resets; update applies deltas
                            if typ.startswith("subscribed"):
                                book["bids"] = {x["price"]: float(x["size"]) for x in ob.get("bids") or []}
                                book["asks"] = {x["price"]: float(x["size"]) for x in ob.get("asks") or []}
                            else:
                                for x in ob.get("bids") or []:
                                    px, sz = x["price"], float(x["size"])
                                    if sz <= 0:
                                        book["bids"].pop(px, None)
                                    else:
                                        book["bids"][px] = sz
                                for x in ob.get("asks") or []:
                                    px, sz = x["price"], float(x["size"])
                                    if sz <= 0:
                                        book["asks"].pop(px, None)
                                    else:
                                        book["asks"][px] = sz
                            bids = sorted(
                                ((float(p), s) for p, s in book["bids"].items()),
                                key=lambda x: -x[0],
                            )
                            asks = sorted(
                                ((float(p), s) for p, s in book["asks"].items()),
                                key=lambda x: x[0],
                            )
                            if bids and asks:
                                self.feat[("lighter", coin)].on_book(now, bids, asks)

                        elif "trade" in typ and coin is not None:
                            if typ.startswith("subscribed"):
                                continue
                            for tr in msg.get("trades") or []:
                                try:
                                    px = float(tr["price"]); sz = float(tr["size"])
                                    ntl = float(tr.get("usd_amount") or px * sz)
                                except Exception:
                                    continue
                                self.feat[("lighter", coin)].on_trade(now, ntl)
                                try:
                                    maker = str(
                                        tr.get("ask_account_id")
                                        if tr.get("is_maker_ask")
                                        else tr.get("bid_account_id")
                                    )
                                    mt = self.lighter_makers.get((coin, maker))
                                    if mt:
                                        mt.maker_fills += 1
                                        got_pnl = False
                                        for k in ("ask_account_pnl", "bid_account_pnl", "pnl"):
                                            if tr.get(k) not in (None, ""):
                                                try:
                                                    mt.est_pnl += float(tr[k])
                                                    got_pnl = True
                                                except Exception:
                                                    pass
                                        if got_pnl:
                                            mt.pnl_known = True
                                except Exception:
                                    pass
                        elif "market_stats" in typ:
                            stats = msg.get("market_stats") or {}
                            sym = stats.get("symbol")
                            if sym in LIGHTER_MARKETS:
                                try:
                                    self.feat[("lighter", sym)].funding = float(
                                        stats.get("funding_rate") or 0
                                    )
                                except Exception:
                                    pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._note_err("lighter_ws", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def _hl_ws_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    HL_WS, ping_interval=20, ping_timeout=20, max_size=8_000_000
                ) as ws:
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
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
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
                            self.feat[("hl", coin)].on_book(now, bids, asks)
                            if coin in self.last_hl_funding:
                                self.feat[("hl", coin)].funding = self.last_hl_funding[coin]
                        elif ch == "activeAssetCtx" and isinstance(data, dict):
                            # {"coin":..., "ctx": {... funding ...}}
                            coin = data.get("coin")
                            ctx = data.get("ctx") or {}
                            if coin in HL_COINS and "funding" in ctx:
                                try:
                                    fr = float(ctx["funding"])
                                    self.last_hl_funding[coin] = fr
                                    self.feat[("hl", coin)].funding = fr
                                except Exception:
                                    pass
                        elif ch == "trades" and isinstance(data, list):
                            for tr in data:
                                coin = tr.get("coin")
                                if coin not in HL_COINS:
                                    continue
                                try:
                                    px = float(tr["px"]); sz = float(tr["sz"])
                                except Exception:
                                    continue
                                self.n_hl_trades += 1
                                self.feat[("hl", coin)].on_trade(now, px * sz)
                                users = tr.get("users") or []
                                maker = hl_trade_maker(tr.get("side"), users)
                                if maker:
                                    self.hl_addrs[maker] = now
                                    self.hl_addr_score[maker] += px * sz
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._note_err("hl_ws", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def _hl_addr_loop(self) -> None:
        """Hot makers polled often; cold less often. Round-robin within budget."""
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._poll_hl_addrs_smart)
            except Exception as e:
                self._note_err("hl_addr", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HL_ADDR_HOT_POLL_S)
            except asyncio.TimeoutError:
                pass

    def _poll_hl_addrs_smart(self) -> None:
        now = time.time()
        # funding via REST backup — sparse (WS activeAssetCtx is primary)
        if now - self.last_funding_rest_t >= 120.0:
            try:
                mac = _http_json(HL_INFO, {"type": "metaAndAssetCtxs"}, timeout=12)
                names = [u["name"] for u in mac[0]["universe"]]
                for coin in HL_COINS:
                    if coin in names:
                        i = names.index(coin)
                        self.last_hl_funding[coin] = float(mac[1][i].get("funding") or 0)
                        self.feat[("hl", coin)].funding = self.last_hl_funding[coin]
                self.last_funding_rest_t = now
            except Exception as e:
                self._note_err("hl_funding", e)
                self.last_funding_rest_t = now  # back off even on error

        scored = sorted(self.hl_addr_score.items(), key=lambda kv: -kv[1])
        hot = [a for a, _ in scored[:HL_HOT_MAKERS]]
        cold = [a for a, _ in scored[HL_HOT_MAKERS:MAX_TRACKED_HL]]
        # always poll hot; rotate a few cold each cycle
        n_cold = max(1, min(4, len(cold)))
        start = self._hl_poll_cursor % max(1, len(cold)) if cold else 0
        batch_cold = cold[start:start + n_cold] + cold[: max(0, n_cold - (len(cold) - start))]
        self._hl_poll_cursor = start + n_cold
        addrs = hot + batch_cold
        for addr in addrs:
            self.n_hl_addr_polls += 1
            try:
                oo = _http_json(HL_INFO, {"type": "frontendOpenOrders", "user": addr}, timeout=12)
            except Exception as e:
                self._note_err("hl_oo", e)
                continue
            by_coin: Dict[str, Dict[str, Any]] = defaultdict(
                lambda: {"bids": [], "asks": [], "oids": set()}
            )
            if isinstance(oo, list):
                for o in oo:
                    if is_hl_trigger_order(o):
                        continue
                    coin = o.get("coin")
                    if coin not in HL_COINS:
                        continue
                    try:
                        px = float(o["limitPx"]); sz = float(o["sz"])
                    except Exception:
                        continue
                    oid = str(o.get("oid") or f"{px}:{sz}")
                    if o.get("side") == "B":
                        by_coin[coin]["bids"].append((px, sz))
                        by_coin[coin]["oids"].add(f"B:{oid}")
                    else:
                        by_coin[coin]["asks"].append((px, sz))
                        by_coin[coin]["oids"].add(f"A:{oid}")
            for coin, sides in by_coin.items():
                feat = self.feat[("hl", coin)]
                mid = None
                if feat.best_bid and feat.best_ask:
                    mid = (feat.best_bid + feat.best_ask) / 2.0
                if mid is None:
                    continue
                tr = self._get_hl_tracker(coin, addr)
                snap = tr.observe_orders(
                    now, mid, sides["bids"], sides["asks"], frozenset(sides["oids"])
                )
                try:
                    self.store.insert_maker_snap(snap)
                except sqlite3.Error as e:
                    self._note_err("hl_db", e)
            # fills sparsely: hot only, every 5th cycle
            if addr in hot and self._hl_fills_tick % 5 == 0:
                try:
                    fills = _http_json(HL_INFO, {"type": "userFills", "user": addr}, timeout=12)
                    if isinstance(fills, list):
                        # Prefer uncrossed (maker) BTC/HYPE fills. No fallback to
                        # taker or other coins; leave pnl_known False if none.
                        maker_fills = [
                            f for f in fills
                            if isinstance(f, dict)
                            and f.get("crossed") is False
                            and f.get("coin") in HL_COINS
                        ]
                        if not maker_fills:
                            continue
                        use = maker_fills[:500]
                        pnl = sum(float(f.get("closedPnl") or 0) for f in use)
                        n_maker = len(use)
                        self.hl_acct_pnl[addr.lower()] = (pnl, n_maker)
                        for coin in HL_COINS:
                            tr = self.hl_makers.get((coin, addr.lower()))
                            if tr:
                                tr.pnl_known = True
                                tr.maker_fills = max(tr.maker_fills, n_maker)
                                tr.est_pnl = pnl
                except Exception as e:
                    self._note_err("hl_fills", e)
            time.sleep(0.35)
        self._hl_fills_tick += 1
        try:
            self.store.commit()
        except sqlite3.Error as e:
            self._note_err("hl_db", e)

    async def _market_snap_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            for (venue, coin), feat in list(self.feat.items()):
                snap = feat.snapshot(now)
                snap["venue"] = venue
                snap["coin"] = coin
                if snap.get("mid") is not None:
                    self.store.insert_market(snap)
            self.store.commit()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=MARKET_SNAP_S)
            except asyncio.TimeoutError:
                pass

    async def _summary_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            for tr in list(self.lighter_makers.values()) + list(self.hl_makers.values()):
                row = tr.summary()
                self.store.upsert_maker_stats(row)
                hist = dict(row)
                hist["t"] = now
                self.store.insert_maker_hist(hist)
            # scrub stale viable flags (e.g. near_bps outliers from earlier rules)
            try:
                self.store.scrub_stale_viable(
                    VIABLE_MIN_DIST_BPS,
                    VIABLE_MAX_DIST_BPS,
                    VIABLE_MIN_CYCLE_S,
                    VIABLE_MIN_SIZE_USD,
                    VIABLE_MAX_PAUSE_FRAC,
                )
            except Exception as e:
                self._note_err("scrub", e)
            self.store.commit()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=MAKER_SUMMARY_S)
            except asyncio.TimeoutError:
                pass

    async def _prune_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                self.store.prune(time.time() - ROLLING_WINDOW_S - 86400)

    def status_dict(self) -> Dict[str, Any]:
        viable = []
        promissors = []
        for tr in list(self.lighter_makers.values()) + list(self.hl_makers.values()):
            s = tr.summary()
            if s["viable"]:
                viable.append(s)
            if s.get("promissor"):
                promissors.append(s)
        return {
            "lighter_makers": len(self.lighter_makers),
            "hl_makers": len(self.hl_makers),
            "hl_addrs_seen": len(self.hl_addrs),
            "n_lighter_polls": self.n_lighter_polls,
            "n_lighter_orders": self.n_lighter_orders,
            "n_hl_trades": self.n_hl_trades,
            "n_hl_addr_polls": self.n_hl_addr_polls,
            "viable_count": len(viable),
            "promissor_count": len(promissors),
            "viable_sample": sorted(viable, key=lambda x: -(x.get("median_near_bps") or 0))[:5],
            "promissor_sample": sorted(promissors, key=lambda x: -(x.get("est_pnl") or 0))[:5],
            "api_errors_recent": self._recent_errs(),
            "db_counts": self.store.counts(),
            "funding_hl": dict(self.last_hl_funding),
        }
