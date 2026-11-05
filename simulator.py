"""Perp MM paper simulator: $100 1x account, FIX/CMP caps, queue, funding, recal."""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config import (
    ARMS,
    BASE_REFRESH_S,
    BASE_SPREAD,
    BOOK_DEPTH_FOR_IMB,
    BOOK_DEPTH_FOR_QUEUE,
    CAP_MODES,
    FILLS_PATH,
    HL_COINS,
    HL_FUNDING_PERIOD_S,
    HL_MAKER_FEE,
    HL_MAKER_FEE_2X,
    HL_MIN_ORDER_USD,
    INITIAL_EQUITY,
    INV_CAP_SOFT,
    LATENCY_API_S,
    MAX_REFRESH_S,
    MAX_REFRESH_STEP_S,
    MAX_SPREAD,
    MAX_SPREAD_STEP,
    MID_JUMP_TRIGGER,
    MIN_REFRESH_S,
    MIN_SPREAD,
    ORDER_USD_BASE,
    PNL_HIST_PATH,
    ROLLING_WINDOW_S,
    SIM_STATE_PATH,
    SKEW_CAP_FRAC,
    SKEW_PER_USD,
    SPREAD_NEIGHBORS,
)
from storage import Store, append_jsonl


@dataclass
class Quote:
    bid_px: float
    ask_px: float
    bid_sz: float
    ask_sz: float
    placed_at: float
    effective_at: float
    bid_queue_ahead: float = 0.0
    ask_queue_ahead: float = 0.0
    bid_sz_opt: float = 0.0
    ask_sz_opt: float = 0.0


@dataclass
class PendingAS:
    fill_id: str
    coin: str
    t: float
    mid_at_fill: float
    side: str
    notional: float
    done_1m: bool = False
    done_5m: bool = False


@dataclass
class ArmBook:
    arm: str  # e.g. A_FIX, B_CMP, A5_FIX
    recal_key: str  # A/B/C/D
    mode: str  # FIX | CMP
    spread: float
    refresh_s: float
    usdc: float = INITIAL_EQUITY
    inv: Dict[str, float] = field(default_factory=lambda: {c: 0.0 for c in HL_COINS})
    quotes: Dict[str, Optional[Quote]] = field(
        default_factory=lambda: {c: None for c in HL_COINS}
    )
    fees_paid: float = 0.0
    fees_paid_2x: float = 0.0
    realized_pnl: float = 0.0
    funding_pnl: float = 0.0
    n_fills: int = 0
    n_fills_opt: int = 0
    n_buys: int = 0
    n_sells: int = 0
    volume_usd: float = 0.0
    as_1m_sum: float = 0.0
    as_1m_ntl: float = 0.0
    as_5m_sum: float = 0.0
    as_5m_ntl: float = 0.0
    pending_as: List[PendingAS] = field(default_factory=list)
    avg_entry: Dict[str, float] = field(default_factory=lambda: {c: 0.0 for c in HL_COINS})
    last_refresh: Dict[str, float] = field(default_factory=dict)
    last_recal_at: float = 0.0
    n_recals: int = 0
    paused: bool = False
    neighbor: bool = False

    def equity(self, mids: Dict[str, float]) -> float:
        eq = self.usdc + self.funding_pnl
        for c, q in self.inv.items():
            m = mids.get(c)
            if m is not None:
                eq += q * m
        return eq

    def pnl_net(self, mids: Dict[str, float]) -> float:
        return self.equity(mids) - INITIAL_EQUITY

    def pnl_net_2x(self, mids: Dict[str, float]) -> float:
        return self.pnl_net(mids) - (self.fees_paid_2x - self.fees_paid)

    def ret_pct(self, mids: Dict[str, float]) -> float:
        return (self.equity(mids) / INITIAL_EQUITY - 1.0) * 100.0

    def inv_usd(self, mids: Dict[str, float]) -> float:
        total = 0.0
        for c, q in self.inv.items():
            m = mids.get(c)
            if m is not None:
                total += q * m
        return total

    def cap(self, mids: Dict[str, float]) -> float:
        if self.mode == "CMP":
            # compound: cap tracks equity; floor so we can still place min orders
            return max(self.equity(mids), HL_MIN_ORDER_USD)
        return INITIAL_EQUITY

    def order_usd(self, mids: Dict[str, float]) -> float:
        cap = self.cap(mids)
        raw = ORDER_USD_BASE * (cap / INITIAL_EQUITY)
        return max(HL_MIN_ORDER_USD, raw)


def _imbalance(bids, asks, n: int) -> float:
    bv = sum(s for _, s in bids[:n])
    av = sum(s for _, s in asks[:n])
    tot = bv + av
    return ((bv - av) / tot) if tot > 0 else 0.0


def _queue_at_price(levels, px: float, side: str) -> Tuple[float, float]:
    if not levels:
        return 0.0, 0.0
    mid_ref = levels[0][0]
    snap_eps = mid_ref * 5e-5
    nearest = min(levels, key=lambda ls: abs(ls[0] - px))
    if abs(nearest[0] - px) <= snap_eps:
        px = nearest[0]
    better = same = 0.0
    if side == "bid":
        for p, s in levels:
            if p > px + 1e-12:
                better += s
            elif abs(p - px) <= 1e-8:
                same += s
    else:
        for p, s in levels:
            if p < px - 1e-12:
                better += s
            elif abs(p - px) <= 1e-8:
                same += s
    return better, same


def _build_arm_specs() -> List[Dict[str, Any]]:
    """A/B/C/D × FIX/CMP at 10 bps; A neighbors 5/20 × both modes."""
    specs = []
    for key, meta in ARMS.items():
        for mode in CAP_MODES:
            specs.append({
                "arm": f"{key}_{mode}",
                "recal_key": key,
                "mode": mode,
                "spread": BASE_SPREAD,
                "refresh_s": BASE_REFRESH_S,
                "neighbor": False,
                "period_s": meta["period_s"],
            })
    for s in SPREAD_NEIGHBORS:
        bps = int(round(s * 1e4))
        for mode in CAP_MODES:
            specs.append({
                "arm": f"A{bps}_{mode}",
                "recal_key": "A",
                "mode": mode,
                "spread": s,
                "refresh_s": BASE_REFRESH_S,
                "neighbor": True,
                "period_s": None,
            })
    return specs


class Simulator:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.mids: Dict[str, float] = {}
        self.bbo: Dict[str, Tuple[float, float]] = {}
        self.books_l2: Dict[str, Tuple[List, List]] = {}
        self.imbalance: Dict[str, float] = {}
        self.funding_rate: Dict[str, float] = {}
        self.last_funding_apply = time.time()
        self.started_at = time.time()
        self.n_trades = 0
        self.n_books = 0
        self._fill_seq = 0
        self.books: Dict[str, ArmBook] = {}
        self._periods: Dict[str, Optional[float]] = {}
        for spec in _build_arm_specs():
            b = ArmBook(
                arm=spec["arm"],
                recal_key=spec["recal_key"],
                mode=spec["mode"],
                spread=spec["spread"],
                refresh_s=spec["refresh_s"],
                neighbor=spec["neighbor"],
            )
            b.last_recal_at = self.started_at
            self.books[spec["arm"]] = b
            self._periods[spec["arm"]] = spec["period_s"]
        # fresh start for $100 schema — do not load incompatible old state
        self._maybe_load()

    def _maybe_load(self) -> None:
        if not SIM_STATE_PATH.exists():
            return
        try:
            raw = json.loads(SIM_STATE_PATH.read_text())
            # only restore if schema matches ($100 + mode field)
            sample = next(iter((raw.get("books") or {}).values()), {})
            if sample.get("mode") not in CAP_MODES or raw.get("initial_equity") != INITIAL_EQUITY:
                return
            self.started_at = raw.get("started_at", self.started_at)
            self._fill_seq = raw.get("fill_seq", 0)
            self.n_trades = raw.get("n_trades", 0)
            self.n_books = raw.get("n_books", 0)
            self.last_funding_apply = raw.get("last_funding_apply", self.last_funding_apply)
            self.funding_rate = raw.get("funding_rate", {})
            for key, braw in (raw.get("books") or {}).items():
                if key not in self.books:
                    continue
                b = self.books[key]
                for attr in (
                    "usdc", "fees_paid", "fees_paid_2x", "realized_pnl", "funding_pnl",
                    "n_fills", "n_fills_opt", "n_buys", "n_sells", "volume_usd",
                    "as_1m_sum", "as_1m_ntl", "as_5m_sum", "as_5m_ntl", "n_recals",
                    "paused", "spread", "refresh_s",
                ):
                    if attr in braw:
                        setattr(b, attr, braw[attr])
                b.inv = braw.get("inv", b.inv)
                b.avg_entry = braw.get("avg_entry", b.avg_entry)
                b.last_refresh = braw.get("last_refresh", {})
                b.last_recal_at = braw.get("last_recal_at", self.started_at)
                b.pending_as = [PendingAS(**p) for p in braw.get("pending_as", []) if "coin" in p]
                for c, qraw in (braw.get("quotes") or {}).items():
                    if qraw:
                        defaults = {
                            "bid_queue_ahead": 0.0, "ask_queue_ahead": 0.0,
                            "bid_sz_opt": qraw.get("bid_sz", 0),
                            "ask_sz_opt": qraw.get("ask_sz", 0),
                        }
                        defaults.update(qraw)
                        b.quotes[c] = Quote(**{k: defaults[k] for k in Quote.__dataclass_fields__})
        except Exception:
            pass

    def save(self) -> None:
        payload: Dict[str, Any] = {
            "started_at": self.started_at,
            "saved_at": time.time(),
            "initial_equity": INITIAL_EQUITY,
            "schema": "usd100_fix_cmp_v1",
            "fill_seq": self._fill_seq,
            "n_trades": self.n_trades,
            "n_books": self.n_books,
            "mids": self.mids,
            "bbo": {k: list(v) for k, v in self.bbo.items()},
            "imbalance": self.imbalance,
            "funding_rate": self.funding_rate,
            "last_funding_apply": self.last_funding_apply,
            "latency_api_s": LATENCY_API_S,
            "books": {},
        }
        for key, b in self.books.items():
            cap = b.cap(self.mids) if self.mids else INITIAL_EQUITY
            inv_u = b.inv_usd(self.mids) if self.mids else 0.0
            eq = b.equity(self.mids) if self.mids else b.usdc
            payload["books"][key] = {
                "arm": b.arm, "recal_key": b.recal_key, "mode": b.mode,
                "neighbor": b.neighbor, "spread": b.spread, "refresh_s": b.refresh_s,
                "usdc": b.usdc, "inv": b.inv,
                "fees_paid": b.fees_paid, "fees_paid_2x": b.fees_paid_2x,
                "realized_pnl": b.realized_pnl, "funding_pnl": b.funding_pnl,
                "n_fills": b.n_fills, "n_fills_opt": b.n_fills_opt,
                "n_buys": b.n_buys, "n_sells": b.n_sells, "volume_usd": b.volume_usd,
                "as_1m_sum": b.as_1m_sum, "as_1m_ntl": b.as_1m_ntl,
                "as_5m_sum": b.as_5m_sum, "as_5m_ntl": b.as_5m_ntl,
                "avg_entry": b.avg_entry, "last_refresh": b.last_refresh,
                "last_recal_at": b.last_recal_at, "n_recals": b.n_recals,
                "paused": b.paused,
                "pending_as": [asdict(p) for p in b.pending_as],
                "quotes": {c: (None if q is None else asdict(q)) for c, q in b.quotes.items()},
                "equity": eq,
                "pnl_net": eq - INITIAL_EQUITY,
                "pnl_net_2x": (eq - INITIAL_EQUITY) - (b.fees_paid_2x - b.fees_paid),
                "ret_pct": (eq / INITIAL_EQUITY - 1.0) * 100.0,
                "cap": cap,
                "inv_usd": inv_u,
                "inv_cap_pct": (abs(inv_u) / cap * 100.0) if cap > 0 else 0.0,
                "order_usd": b.order_usd(self.mids) if self.mids else ORDER_USD_BASE,
                "next_recal_s": self._next_recal_s(b),
            }
        tmp = SIM_STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(SIM_STATE_PATH)
        if self.mids:
            row = {"t": time.time()}
            for k, b in self.books.items():
                row[k] = b.pnl_net(self.mids)
            append_jsonl(PNL_HIST_PATH, json.dumps(row))

    def _next_recal_s(self, b: ArmBook) -> Optional[float]:
        period = self._periods.get(b.arm)
        if period is None:
            return None
        return max(0.0, b.last_recal_at + period - time.time())

    def set_funding(self, coin: str, rate: float) -> None:
        self.funding_rate[coin] = rate

    def apply_funding(self, now: float) -> None:
        dt = now - self.last_funding_apply
        if dt < 1.0:
            return
        for book in self.books.values():
            for coin, qty in book.inv.items():
                if abs(qty) < 1e-15:
                    continue
                mid = self.mids.get(coin)
                rate = self.funding_rate.get(coin)
                if mid is None or rate is None:
                    continue
                book.funding_pnl -= qty * mid * rate * (dt / HL_FUNDING_PERIOD_S)
        self.last_funding_apply = now

    def on_book(self, coin, bids, asks, now: float) -> None:
        if not bids or not asks:
            return
        best_bid, best_ask = bids[0][0], asks[0][0]
        if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
            return
        self.bbo[coin] = (best_bid, best_ask)
        self.mids[coin] = (best_bid + best_ask) / 2.0
        self.books_l2[coin] = (bids, asks)
        self.imbalance[coin] = _imbalance(bids, asks, BOOK_DEPTH_FOR_IMB)
        self.n_books += 1
        self.maybe_refresh(now)
        self._resolve_as(now)
        self.apply_funding(now)
        self.maybe_recalibrate(now)

    def on_trade(self, coin, px, sz, side, now: float) -> None:
        if coin not in HL_COINS:
            return
        self.n_trades += 1
        for b in self.books.values():
            self._try_fill(b, coin, px, sz, side, now)

    def _place(self, book: ArmBook, coin: str, now: float) -> None:
        mid = self.mids.get(coin)
        bbo = self.bbo.get(coin)
        if mid is None or mid <= 0 or bbo is None or book.paused:
            return
        cap = book.cap(self.mids)
        inv_u = book.inv_usd(self.mids)
        # side policy at inventory cap
        allow_bid = True
        allow_ask = True
        if inv_u >= INV_CAP_SOFT * cap:
            allow_bid = False  # only reduce (sell)
        elif inv_u <= -INV_CAP_SOFT * cap:
            allow_ask = False  # only reduce (buy)

        order_usd = book.order_usd(self.mids)
        # don't bid more than remaining room to cap
        room_long = max(0.0, cap - inv_u)
        room_short = max(0.0, cap + inv_u)
        bid_usd = min(order_usd, room_long) if allow_bid else 0.0
        ask_usd = min(order_usd, room_short) if allow_ask else 0.0
        if bid_usd > 0 and bid_usd < HL_MIN_ORDER_USD:
            bid_usd = 0.0  # cannot place below HL min
        if ask_usd > 0 and ask_usd < HL_MIN_ORDER_USD:
            ask_usd = 0.0

        inv_usd_coin = book.inv[coin] * mid
        skew = max(
            -SKEW_CAP_FRAC * book.spread,
            min(SKEW_CAP_FRAC * book.spread, inv_usd_coin * SKEW_PER_USD),
        )
        bid_px = mid * (1.0 - (book.spread + skew))
        ask_px = mid * (1.0 + (book.spread - skew))
        best_bid, best_ask = bbo
        if bid_px >= best_ask:
            bid_px = best_bid
        if ask_px <= best_bid:
            ask_px = best_ask
        if bid_px >= ask_px:
            bid_px = mid * (1.0 - book.spread)
            ask_px = mid * (1.0 + book.spread)

        bid_sz = (bid_usd / mid) if bid_usd > 0 else 0.0
        ask_sz = (ask_usd / mid) if ask_usd > 0 else 0.0
        if bid_sz <= 0 and ask_sz <= 0:
            book.quotes[coin] = None
            book.last_refresh[coin] = now
            return

        bids, asks = self.books_l2.get(coin, ([], []))
        b_better, b_same = _queue_at_price(bids[:BOOK_DEPTH_FOR_QUEUE], bid_px, "bid") if bid_sz else (0.0, 0.0)
        a_better, a_same = _queue_at_price(asks[:BOOK_DEPTH_FOR_QUEUE], ask_px, "ask") if ask_sz else (0.0, 0.0)
        book.quotes[coin] = Quote(
            bid_px=bid_px, ask_px=ask_px, bid_sz=bid_sz, ask_sz=ask_sz,
            placed_at=now, effective_at=now + LATENCY_API_S,
            bid_queue_ahead=b_better + b_same,
            ask_queue_ahead=a_better + a_same,
            bid_sz_opt=bid_sz, ask_sz_opt=ask_sz,
        )
        book.last_refresh[coin] = now

    def maybe_refresh(self, now: float, force: bool = False) -> None:
        for book in self.books.values():
            for coin in HL_COINS:
                mid = self.mids.get(coin)
                if mid is None:
                    continue
                q = book.quotes[coin]
                last = book.last_refresh.get(coin, 0.0)
                need = force or q is None or (now - last) >= book.refresh_s
                if q is not None and mid > 0:
                    qmid = (q.bid_px + q.ask_px) / 2.0
                    if abs(mid - qmid) / mid > MID_JUMP_TRIGGER * book.spread:
                        need = True
                        if abs(mid - qmid) / mid > 3 * book.spread:
                            book.paused = True
                if book.paused and (now - last) > max(5.0, book.refresh_s):
                    book.paused = False
                    need = True
                # also refresh when inv crosses soft cap
                if self.mids:
                    cap = book.cap(self.mids)
                    inv_u = book.inv_usd(self.mids)
                    if abs(inv_u) >= INV_CAP_SOFT * cap:
                        need = True
                if need and not book.paused:
                    self._place(book, coin, now)

    def _try_fill(self, book, coin, px, sz, side, now) -> None:
        q = book.quotes.get(coin)
        if q is None or book.paused:
            return
        if now < q.effective_at:
            return
        # optimistic
        if side == "A" and q.bid_sz_opt > 0 and px <= q.bid_px + 1e-12:
            take = min(q.bid_sz_opt, sz)
            q.bid_sz_opt -= take
            if take > 0:
                book.n_fills_opt += 1
        if side == "B" and q.ask_sz_opt > 0 and px >= q.ask_px - 1e-12:
            take = min(q.ask_sz_opt, sz)
            q.ask_sz_opt -= take
            if take > 0:
                book.n_fills_opt += 1
        # queue-aware
        if side == "A" and q.bid_sz > 0 and px <= q.bid_px + 1e-9:
            remain = sz
            if q.bid_queue_ahead > 0:
                eat = min(q.bid_queue_ahead, remain)
                q.bid_queue_ahead -= eat
                remain -= eat
            if remain > 0 and q.bid_queue_ahead <= 1e-15:
                fill_sz = min(q.bid_sz, remain)
                if fill_sz > 0 and self._would_respect_cap(book, coin, "buy", fill_sz, q.bid_px):
                    self._apply_fill(book, coin, "buy", q.bid_px, fill_sz, now)
                    q.bid_sz -= fill_sz
                    if q.bid_sz <= 1e-12:
                        q.bid_sz = 0.0
        elif side == "A" and q.bid_sz > 0 and px > q.bid_px:
            if q.bid_queue_ahead > 0:
                q.bid_queue_ahead = max(0.0, q.bid_queue_ahead - sz)
        if side == "B" and q.ask_sz > 0 and px >= q.ask_px - 1e-9:
            remain = sz
            if q.ask_queue_ahead > 0:
                eat = min(q.ask_queue_ahead, remain)
                q.ask_queue_ahead -= eat
                remain -= eat
            if remain > 0 and q.ask_queue_ahead <= 1e-15:
                fill_sz = min(q.ask_sz, remain)
                if fill_sz > 0 and self._would_respect_cap(book, coin, "sell", fill_sz, q.ask_px):
                    self._apply_fill(book, coin, "sell", q.ask_px, fill_sz, now)
                    q.ask_sz -= fill_sz
                    if q.ask_sz <= 1e-12:
                        q.ask_sz = 0.0
        elif side == "B" and q.ask_sz > 0 and px < q.ask_px:
            if q.ask_queue_ahead > 0:
                q.ask_queue_ahead = max(0.0, q.ask_queue_ahead - sz)
        if q is not None and q.bid_sz <= 0 and q.ask_sz <= 0:
            self._place(book, coin, now)

    def _would_respect_cap(self, book, coin, side, sz, px) -> bool:
        # projected inv usd after fill
        inv_u = book.inv_usd(self.mids)
        delta = sz * px if side == "buy" else -sz * px
        cap = book.cap(self.mids)
        return abs(inv_u + delta) <= cap * 1.001  # tiny epsilon

    def _apply_fill(self, book, coin, our_side, px, sz, now, queue: bool = True) -> None:
        ntl = px * sz
        fee = ntl * HL_MAKER_FEE
        fee2 = ntl * HL_MAKER_FEE_2X
        if our_side == "buy":
            book.usdc -= ntl + fee
            old = book.inv[coin]
            new = old + sz
            if new != 0:
                book.avg_entry[coin] = (
                    (book.avg_entry[coin] * old + px * sz) / new if old > 0 else px
                )
            book.inv[coin] = new
            book.n_buys += 1
        else:
            book.usdc += ntl - fee
            old = book.inv[coin]
            if old > 0:
                book.realized_pnl += (px - book.avg_entry[coin]) * min(sz, old)
            book.inv[coin] = old - sz
            if abs(book.inv[coin]) < 1e-12:
                book.inv[coin] = 0.0
                book.avg_entry[coin] = 0.0
            book.n_sells += 1
        book.fees_paid += fee
        book.fees_paid_2x += fee2
        book.n_fills += 1
        book.volume_usd += ntl
        self._fill_seq += 1
        fid = str(self._fill_seq)
        mid = self.mids.get(coin, px)
        book.pending_as.append(
            PendingAS(fill_id=fid, coin=coin, t=now, mid_at_fill=mid, side=our_side, notional=ntl)
        )
        append_jsonl(FILLS_PATH, json.dumps({
            "id": fid, "t": now, "arm": book.arm, "mode": book.mode,
            "spread": book.spread, "refresh_s": book.refresh_s, "coin": coin,
            "side": our_side, "px": px, "sz": sz, "notional": ntl, "fee": fee,
            "mid": mid, "inv_after": book.inv[coin], "latency_s": LATENCY_API_S,
            "queue_aware": queue, "cap": book.cap(self.mids),
            "equity": book.equity(self.mids),
        }))

    def _resolve_as(self, now: float) -> None:
        for book in self.books.values():
            still = []
            for p in book.pending_as:
                mid = self.mids.get(p.coin)
                if mid is None:
                    still.append(p)
                    continue
                sign = 1.0 if p.side == "buy" else -1.0
                move = (mid - p.mid_at_fill) / p.mid_at_fill if p.mid_at_fill else 0.0
                adv = -sign * move
                if not p.done_1m and now - p.t >= 60:
                    book.as_1m_sum += adv * p.notional
                    book.as_1m_ntl += p.notional
                    p.done_1m = True
                if not p.done_5m and now - p.t >= 300:
                    book.as_5m_sum += adv * p.notional
                    book.as_5m_ntl += p.notional
                    p.done_5m = True
                if not (p.done_1m and p.done_5m):
                    still.append(p)
            book.pending_as = still

    def maybe_recalibrate(self, now: float) -> None:
        since = now - ROLLING_WINDOW_S
        # recalibrate each recal_key once, apply to both FIX and CMP copies
        done_keys = set()
        for arm_name, book in self.books.items():
            if book.neighbor:
                continue
            key = book.recal_key
            if key in done_keys:
                continue
            period = self._periods.get(arm_name)
            if period is None:
                continue
            # use FIX copy as timer reference
            ref = self.books.get(f"{key}_FIX") or book
            if now - ref.last_recal_at < period:
                continue
            target = self._fit_from_observer(since)
            done_keys.add(key)
            for mode in CAP_MODES:
                b = self.books.get(f"{key}_{mode}")
                if b is None:
                    continue
                if target is None:
                    b.last_recal_at = now
                    continue
                new_spread, new_refresh = target
                ds = max(-MAX_SPREAD_STEP, min(MAX_SPREAD_STEP, new_spread - b.spread))
                dr = max(-MAX_REFRESH_STEP_S, min(MAX_REFRESH_STEP_S, new_refresh - b.refresh_s))
                b.spread = max(MIN_SPREAD, min(MAX_SPREAD, b.spread + ds))
                b.refresh_s = max(MIN_REFRESH_S, min(MAX_REFRESH_S, b.refresh_s + dr))
                b.last_recal_at = now
                b.n_recals += 1
            self.store.insert_event(
                now, "recal", json.dumps({"recal_key": key, "target": target, "n_promissor": getattr(self, "_last_n_promissor", None)})
            )
            self.store.commit()

    def _fit_from_observer(self, since: float):
        """Median of PROMISSOR makers only (viable + PnL>0 or activity proxy)."""
        makers = self.store.promissor_makers(since)
        # one vote per account per param set: same venue+account on BTC and HYPE
        # with ~equal near/cycle counts once; different params count per (★) market row.
        seen = set()
        uniq = []
        for m in makers:
            near = m.get("median_near_bps")
            cyc = m.get("median_cycle_s")
            key = (
                m.get("venue"), m.get("maker_id"),
                round(near) if near is not None else None,
                round(cyc) if cyc is not None else None,
            )
            if key in seen:
                continue
            seen.add(key)
            uniq.append(m)
        makers = uniq
        self._last_n_promissor = len(makers)
        if not makers:
            return None  # no param change if nobody passes the filter
        spreads = [m["median_near_bps"] / 1e4 for m in makers if m.get("median_near_bps") is not None]
        cycles = [m["median_cycle_s"] for m in makers if m.get("median_cycle_s") is not None]
        if not spreads or not cycles:
            return None
        regimes = self.store.recent_market_regimes(max(since, time.time() - 3600))
        rvols = [r[1] for r in regimes if r[1] is not None]
        vol_scale = 1.0
        if rvols:
            med = statistics.median(rvols)
            if med > 0.0008:
                vol_scale = 1.25
            elif med < 0.0003:
                vol_scale = 0.9
        return statistics.median(spreads) * vol_scale, max(3.0, statistics.median(cycles))
