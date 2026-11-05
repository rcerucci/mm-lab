"""Perp MM paper simulator: $100 1x account, FIX/CMP caps, queue, funding, recal."""
from __future__ import annotations

import json
import shutil
import statistics
import time
from collections import deque
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
    RECAL_MIN_SPREAD,
    ORDER_USD_BASE,
    PNL_HIST_PATH,
    ROLLING_WINDOW_S,
    SIM_STATE_PATH,
    SKEW_CAP_FRAC,
    SKEW_PER_USD,
    SPREAD_NEIGHBORS,
    AS_TOLERANCE_S,
    MID_GAP_S,
    VERSION_PATH,
)
from storage import Store, append_jsonl


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} America/Sao_Paulo] {msg}", flush=True)


def read_git_rev() -> str:
    try:
        if VERSION_PATH.exists():
            return VERSION_PATH.read_text().strip().split()[0][:40]
    except Exception:
        pass
    return "unknown"


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
    retire_at: Optional[float] = None
    ref_mid: Optional[float] = None
    bid_sz_tt: float = 0.0
    ask_sz_tt: float = 0.0
    beyond_visible_bid: bool = False
    beyond_visible_ask: bool = False


@dataclass
class PendingAS:
    fill_id: str
    coin: str
    t: float
    mid_at_fill: float
    side: str
    notional: float
    through: bool = False
    done_1s: bool = False
    done_5s: bool = False
    done_30s: bool = False
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
    quotes: Dict[str, List[Quote]] = field(
        default_factory=lambda: {c: [] for c in HL_COINS}
    )
    fees_paid: float = 0.0
    fees_paid_2x: float = 0.0
    realized_pnl: float = 0.0
    funding_pnl: float = 0.0
    n_fills: int = 0
    n_fills_opt: int = 0
    n_fills_tt: int = 0
    volume_tt: float = 0.0
    n_fills_through: int = 0
    volume_through: float = 0.0
    n_replaces: Dict[str, int] = field(default_factory=lambda: {c: 0 for c in HL_COINS})
    n_cap_overshoot: int = 0
    max_cap_overshoot: float = 0.0
    n_mid_before_fallback: int = 0
    n_quotes_placed: Dict[str, int] = field(default_factory=lambda: {c: 0 for c in HL_COINS})
    n_quotes_beyond: Dict[str, int] = field(default_factory=lambda: {c: 0 for c in HL_COINS})
    n_buys: int = 0
    n_sells: int = 0
    volume_usd: float = 0.0
    as_1s_sum: float = 0.0
    as_1s_ntl: float = 0.0
    as_5s_sum: float = 0.0
    as_5s_ntl: float = 0.0
    as_30s_sum: float = 0.0
    as_30s_ntl: float = 0.0
    as_1m_sum: float = 0.0
    as_1m_ntl: float = 0.0
    as_5m_sum: float = 0.0
    as_5m_ntl: float = 0.0
    pending_as: List[PendingAS] = field(default_factory=list)
    pending_as_tt: List[PendingAS] = field(default_factory=list)
    as_tt_sum: Dict[str, float] = field(
        default_factory=lambda: {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")}
    )
    as_tt_ntl: Dict[str, float] = field(
        default_factory=lambda: {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")}
    )
    as_seen: Dict[str, int] = field(
        default_factory=lambda: {k: 0 for k in ("1s", "5s", "30s", "1m", "5m")}
    )
    as_discard: Dict[str, int] = field(
        default_factory=lambda: {k: 0 for k in ("1s", "5s", "30s", "1m", "5m")}
    )
    avg_entry: Dict[str, float] = field(default_factory=lambda: {c: 0.0 for c in HL_COINS})
    last_refresh: Dict[str, float] = field(default_factory=dict)
    last_recal_at: float = 0.0
    as_short_since: float = 0.0  # when 1s/5s/30s AS started being measured
    n_recals: int = 0
    paused_until: Dict[str, float] = field(default_factory=dict)
    allowed_sides: Dict[str, Tuple[bool, bool]] = field(default_factory=dict)
    neighbor: bool = False
    intentionally_flat: Dict[str, bool] = field(default_factory=dict)
    n_pauses: Dict[str, int] = field(default_factory=lambda: {c: 0 for c in HL_COINS})
    replace_reasons: Dict[str, Dict[str, int]] = field(
        default_factory=lambda: {
            c: {"timer": 0, "jump": 0, "sides_edge": 0, "pause_resume": 0, "post_fill": 0}
            for c in HL_COINS
        }
    )
    as_coin_sum: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {c: {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")} for c in HL_COINS}
    )
    as_coin_ntl: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {c: {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")} for c in HL_COINS}
    )
    as_through_sum: Dict[str, float] = field(
        default_factory=lambda: {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")}
    )
    as_through_ntl: Dict[str, float] = field(
        default_factory=lambda: {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")}
    )

    def paused_now(self, now: float) -> bool:
        return any(now < self.paused_until.get(c, 0.0) for c in HL_COINS)

    def coin_paused(self, coin: str, now: float) -> bool:
        return now < self.paused_until.get(coin, 0.0)

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
        """Signed net inventory in USD (display / skew)."""
        total = 0.0
        for c, q in self.inv.items():
            m = mids.get(c)
            if m is not None:
                total += q * m
        return total

    def inv_usd_gross(self, mids: Dict[str, float]) -> float:
        """Sum of |qty * mid| across coins — the 1x usage of the account."""
        total = 0.0
        for c, q in self.inv.items():
            m = mids.get(c)
            if m is not None:
                total += abs(q * m)
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


REPLACE_REASONS = ("timer", "jump", "sides_edge", "pause_resume", "post_fill")


def _bump_reason(book: ArmBook, coin: str, reason: str) -> None:
    rec = book.replace_reasons.setdefault(
        coin, {r: 0 for r in REPLACE_REASONS}
    )
    rec[reason] = rec.get(reason, 0) + 1


def _primary_reason(reasons) -> str:
    for cand in ("post_fill", "jump", "sides_edge", "pause_resume", "timer"):
        if cand in reasons:
            return cand
    return "timer"


def _beyond_visible(levels, px: float, side: str) -> bool:
    if not levels:
        return True
    if side == "bid":
        return px < levels[-1][0] - 1e-12
    return px > levels[-1][0] + 1e-12


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
        self.mid_hist: Dict[str, deque] = {c: deque(maxlen=2400) for c in HL_COINS}
        self.funding_rate: Dict[str, float] = {}
        self.last_funding_apply = time.time()
        self.started_at = time.time()
        self.n_trades = 0
        self.n_books = 0
        self._fill_seq = 0
        self._seen_tid_set = set()
        self._seen_tid_q: deque = deque()
        self._ws_reconnects = 0
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
        import copy
        _fresh_snapshot = copy.deepcopy({k: v for k, v in self.__dict__.items() if k != "store"})
        try:
            raw = json.loads(SIM_STATE_PATH.read_text())
            # only restore if schema matches ($100 + mode field)
            sample = next(iter((raw.get("books") or {}).values()), {})
            if sample.get("mode") not in CAP_MODES or raw.get("initial_equity") != INITIAL_EQUITY:
                bak = self._backup_state()
                _log(
                    f"sim state NOT loaded: schema mismatch (mode={sample.get('mode')!r}, "
                    f"initial_equity={raw.get('initial_equity')!r}); starting fresh; backup={bak}"
                )
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
                    "n_fills", "n_fills_opt", "n_fills_tt", "volume_tt",
                    "n_fills_through", "volume_through",
                    "n_buys", "n_sells", "volume_usd",
                    "as_1s_sum", "as_1s_ntl", "as_5s_sum", "as_5s_ntl",
                    "as_30s_sum", "as_30s_ntl",
                    "as_1m_sum", "as_1m_ntl", "as_5m_sum", "as_5m_ntl", "n_recals",
                    "n_cap_overshoot", "max_cap_overshoot",
                    "n_mid_before_fallback",
                    "spread", "refresh_s",
                ):
                    if attr in braw:
                        setattr(b, attr, braw[attr])
                b.inv = braw.get("inv", b.inv)
                b.avg_entry = braw.get("avg_entry", b.avg_entry)
                b.last_refresh = braw.get("last_refresh", {})
                b.last_recal_at = braw.get("last_recal_at", self.started_at)
                if "paused_until" in braw and isinstance(braw["paused_until"], dict):
                    b.paused_until = {k: float(v) for k, v in braw["paused_until"].items()}
                elif braw.get("paused"):
                    # old global flag: treat as still paused on every coin for one cooldown
                    until = time.time() + max(5.0, b.refresh_s)
                    b.paused_until = {c: until for c in HL_COINS}
                if isinstance(braw.get("n_replaces"), dict):
                    b.n_replaces = {c: int(braw["n_replaces"].get(c, 0)) for c in HL_COINS}
                if "n_fills_through" not in braw and "n_fills_tt" in braw:
                    b.n_fills_through = int(braw.get("n_fills_tt") or 0)
                    b.volume_through = float(braw.get("volume_tt") or 0)
                if isinstance(braw.get("n_pauses"), dict):
                    b.n_pauses = {c: int(braw["n_pauses"].get(c, 0)) for c in HL_COINS}
                if isinstance(braw.get("replace_reasons"), dict):
                    for c in HL_COINS:
                        src = braw["replace_reasons"].get(c) or {}
                        rec = b.replace_reasons.setdefault(c, {r: 0 for r in REPLACE_REASONS})
                        for r, v in src.items():
                            rec[r] = int(v)
                if isinstance(braw.get("intentionally_flat"), dict):
                    b.intentionally_flat = {c: bool(v) for c, v in braw["intentionally_flat"].items()}
                if isinstance(braw.get("n_quotes_placed"), dict):
                    b.n_quotes_placed = {c: int(braw["n_quotes_placed"].get(c, 0)) for c in HL_COINS}
                if isinstance(braw.get("n_quotes_beyond"), dict):
                    b.n_quotes_beyond = {c: int(braw["n_quotes_beyond"].get(c, 0)) for c in HL_COINS}
                if isinstance(braw.get("as_coin_sum"), dict):
                    for c, hv in braw["as_coin_sum"].items():
                        if isinstance(hv, dict):
                            b.as_coin_sum.setdefault(c, {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")})
                            b.as_coin_sum[c].update({k: float(v) for k, v in hv.items()})
                if isinstance(braw.get("as_coin_ntl"), dict):
                    for c, hv in braw["as_coin_ntl"].items():
                        if isinstance(hv, dict):
                            b.as_coin_ntl.setdefault(c, {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")})
                            b.as_coin_ntl[c].update({k: float(v) for k, v in hv.items()})
                if isinstance(braw.get("as_through_sum"), dict):
                    b.as_through_sum.update({k: float(v) for k, v in braw["as_through_sum"].items()})
                if isinstance(braw.get("as_through_ntl"), dict):
                    b.as_through_ntl.update({k: float(v) for k, v in braw["as_through_ntl"].items()})
                if isinstance(braw.get("as_seen"), dict):
                    b.as_seen.update({k: int(v) for k, v in braw["as_seen"].items()})
                if isinstance(braw.get("as_discard"), dict):
                    b.as_discard.update({k: int(v) for k, v in braw["as_discard"].items()})
                if isinstance(braw.get("as_tt_sum"), dict):
                    b.as_tt_sum.update({k: float(v) for k, v in braw["as_tt_sum"].items()})
                if isinstance(braw.get("as_tt_ntl"), dict):
                    b.as_tt_ntl.update({k: float(v) for k, v in braw["as_tt_ntl"].items()})
                _pf = PendingAS.__dataclass_fields__
                b.pending_as = []
                for p in braw.get("pending_as", []):
                    if "coin" not in p:
                        continue
                    pa = PendingAS(**{k: v for k, v in p.items() if k in _pf})
                    if "done_1s" not in p:
                        # pre-upgrade fill: its 1s/5s/30s mid was never sampled;
                        # skip those horizons instead of using a stale late mid
                        pa.done_1s = pa.done_5s = pa.done_30s = True
                    b.pending_as.append(pa)
                if "as_short_since" in braw:
                    b.as_short_since = braw["as_short_since"]
                else:
                    b.as_short_since = time.time()
                    for tag in ("1s", "5s", "30s"):
                        setattr(b, f"as_{tag}_sum", 0.0)
                        setattr(b, f"as_{tag}_ntl", 0.0)
                def _load_quote(qraw):
                    if not qraw:
                        return None
                    defaults = {
                        "bid_queue_ahead": 0.0, "ask_queue_ahead": 0.0,
                        "bid_sz_opt": qraw.get("bid_sz", 0),
                        "ask_sz_opt": qraw.get("ask_sz", 0),
                        "retire_at": None,
                        "ref_mid": None,
                        "bid_sz_tt": qraw.get("bid_sz", 0),
                        "ask_sz_tt": qraw.get("ask_sz", 0),
                        "beyond_visible_bid": False,
                        "beyond_visible_ask": False,
                    }
                    defaults.update(qraw)
                    return Quote(**{k: defaults[k] for k in Quote.__dataclass_fields__})
                def _load_quote_list(raw):
                    if not raw:
                        return []
                    if isinstance(raw, dict):
                        q = _load_quote(raw)
                        return [q] if q is not None else []
                    out = []
                    for item in raw:
                        q = _load_quote(item)
                        if q is not None:
                            out.append(q)
                    return out
                for c in HL_COINS:
                    qs = _load_quote_list((braw.get("quotes") or {}).get(c))
                    stale = _load_quote((braw.get("stale") or {}).get(c))
                    if stale is not None:
                        qs = [stale] + qs
                    b.quotes[c] = qs
                b.pending_as_tt = []
                for p in braw.get("pending_as_tt", []):
                    if "coin" not in p:
                        continue
                    b.pending_as_tt.append(PendingAS(**{k: v for k, v in p.items() if k in _pf}))
        except Exception as e:  # noqa: BLE001
            bak = self._backup_state()
            _log(f"sim state LOAD FAILED: {type(e).__name__}: {e}; starting fresh; backup={bak}")
            # reset any partially loaded books to fresh state
            self.__dict__.update(_fresh_snapshot)
            return

    @staticmethod
    def _backup_state() -> Optional[str]:
        try:
            bak = SIM_STATE_PATH.with_name(f"{SIM_STATE_PATH.name}.bak-{int(time.time())}")
            shutil.copy2(SIM_STATE_PATH, bak)
            return str(bak)
        except Exception as e:  # noqa: BLE001
            _log(f"sim state backup FAILED: {type(e).__name__}: {e}")
            return None

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
            "git_rev": read_git_rev(),
            "books": {},
        }
        for key, b in self.books.items():
            cap = b.cap(self.mids) if self.mids else INITIAL_EQUITY
            inv_u = b.inv_usd(self.mids) if self.mids else 0.0
            inv_g = b.inv_usd_gross(self.mids) if self.mids else 0.0
            eq = b.equity(self.mids) if self.mids else b.usdc
            payload["books"][key] = {
                "arm": b.arm, "recal_key": b.recal_key, "mode": b.mode,
                "neighbor": b.neighbor, "spread": b.spread, "refresh_s": b.refresh_s,
                "usdc": b.usdc, "inv": b.inv,
                "fees_paid": b.fees_paid, "fees_paid_2x": b.fees_paid_2x,
                "realized_pnl": b.realized_pnl, "funding_pnl": b.funding_pnl,
                "n_fills": b.n_fills, "n_fills_opt": b.n_fills_opt,
                "n_fills_tt": b.n_fills_through, "volume_tt": b.volume_through,
                "n_fills_through": b.n_fills_through, "volume_through": b.volume_through,
                "n_replaces": b.n_replaces,
                "replace_reasons": b.replace_reasons,
                "n_pauses": b.n_pauses,
                "intentionally_flat": b.intentionally_flat,
                "n_cap_overshoot": b.n_cap_overshoot,
                "max_cap_overshoot": b.max_cap_overshoot,
                "n_mid_before_fallback": b.n_mid_before_fallback,
                "n_quotes_placed": b.n_quotes_placed,
                "n_quotes_beyond": b.n_quotes_beyond,
                "n_buys": b.n_buys, "n_sells": b.n_sells, "volume_usd": b.volume_usd,
                "as_1s_sum": b.as_1s_sum, "as_1s_ntl": b.as_1s_ntl,
                "as_5s_sum": b.as_5s_sum, "as_5s_ntl": b.as_5s_ntl,
                "as_30s_sum": b.as_30s_sum, "as_30s_ntl": b.as_30s_ntl,
                "as_short_since": b.as_short_since,
                "as_1m_sum": b.as_1m_sum, "as_1m_ntl": b.as_1m_ntl,
                "as_5m_sum": b.as_5m_sum, "as_5m_ntl": b.as_5m_ntl,
                "avg_entry": b.avg_entry, "last_refresh": b.last_refresh,
                "last_recal_at": b.last_recal_at, "n_recals": b.n_recals,
                "paused": b.paused_now(time.time()),
                "paused_until": b.paused_until,
                "pending_as": [asdict(p) for p in b.pending_as],
                "pending_as_tt": [asdict(p) for p in b.pending_as_tt],
                "as_seen": b.as_seen, "as_discard": b.as_discard,
                "as_tt_sum": b.as_through_sum, "as_tt_ntl": b.as_through_ntl,
                "as_through_sum": b.as_through_sum, "as_through_ntl": b.as_through_ntl,
                "as_coin_sum": b.as_coin_sum, "as_coin_ntl": b.as_coin_ntl,
                "as_coverage": {
                    k: (0.0 if not b.as_seen.get(k) else
                        1.0 - b.as_discard.get(k, 0) / b.as_seen[k])
                    for k in ("1s", "5s", "30s", "1m", "5m")
                },
                "quotes": {c: [asdict(q) for q in (b.quotes.get(c) or [])] for c in HL_COINS},
                "equity": eq,
                "pnl_net": eq - INITIAL_EQUITY,
                "pnl_net_2x": (eq - INITIAL_EQUITY) - (b.fees_paid_2x - b.fees_paid),
                "ret_pct": (eq / INITIAL_EQUITY - 1.0) * 100.0,
                "cap": cap,
                "inv_usd": inv_u,
                "inv_gross": inv_g,
                "inv_cap_pct": (inv_g / cap * 100.0) if cap > 0 else 0.0,
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
        self.mid_hist.setdefault(coin, deque(maxlen=2400)).append((now, self.mids[coin]))
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

    def note_tid(self, tid) -> bool:
        """True if this exchange trade id is new and should be processed."""
        if tid is None:
            return True
        if tid in self._seen_tid_set:
            return False
        self._seen_tid_set.add(tid)
        self._seen_tid_q.append(tid)
        if len(self._seen_tid_q) > 8000:
            old = self._seen_tid_q.popleft()
            self._seen_tid_set.discard(old)
        return True

    def on_ws_reconnect(self, now: float) -> None:
        self._ws_reconnects += 1
        for book in self.books.values():
            for coin in HL_COINS:
                self._retire_quote(book, coin, now)
                book.intentionally_flat[coin] = False

    def _current_quote(self, book: ArmBook, coin: str):
        qs = book.quotes.get(coin) or []
        for q in reversed(qs):
            if q.retire_at is None:
                return q
        return None

    def _retire_quote(self, book: ArmBook, coin: str, now: float) -> None:
        for q in (book.quotes.get(coin) or []):
            if q.retire_at is None:
                q.retire_at = now + LATENCY_API_S

    def _sweep_expired(self, book: ArmBook, coin: str, now: float) -> None:
        qs = book.quotes.get(coin) or []
        book.quotes[coin] = [
            q for q in qs
            if not (q.retire_at is not None and now >= q.retire_at)
        ]

    def _fillable_quotes(self, book: ArmBook, coin: str, now: float):
        self._sweep_expired(book, coin, now)
        out = []
        for q in (book.quotes.get(coin) or []):
            if now < q.effective_at:
                continue
            if q.retire_at is not None and now >= q.retire_at:
                continue
            out.append(q)
        return out

    def _allowed_sides(self, book: ArmBook, coin: str):
        mid = self.mids.get(coin)
        if mid is None or mid <= 0:
            return True, True
        cap = book.cap(self.mids)
        gross = book.inv_usd_gross(self.mids)
        inv_coin = book.inv[coin] * mid
        allow_bid = True
        allow_ask = True
        if gross >= INV_CAP_SOFT * cap:
            if inv_coin > 1e-12:
                allow_bid = False
            elif inv_coin < -1e-12:
                allow_ask = False
            else:
                allow_bid = False
                allow_ask = False
        return allow_bid, allow_ask

    def _place(self, book: ArmBook, coin: str, now: float, reason: str = "timer") -> None:
        mid = self.mids.get(coin)
        bbo = self.bbo.get(coin)
        if mid is None or mid <= 0 or bbo is None or book.coin_paused(coin, now):
            return
        cap = book.cap(self.mids)
        gross = book.inv_usd_gross(self.mids)
        inv_coin = book.inv[coin] * mid
        allow_bid, allow_ask = self._allowed_sides(book, coin)
        book.allowed_sides[coin] = (allow_bid, allow_ask)

        order_usd = book.order_usd(self.mids)
        room = max(0.0, cap - gross)
        # reducing side: size with min(order, position) and allow below HL min
        if allow_bid:
            if book.inv[coin] < 0:
                bid_usd = min(order_usd, abs(inv_coin))
            else:
                bid_usd = min(order_usd, room)
        else:
            bid_usd = 0.0
        if allow_ask:
            if book.inv[coin] > 0:
                ask_usd = min(order_usd, abs(inv_coin))
            else:
                ask_usd = min(order_usd, room)
        else:
            ask_usd = 0.0
        reducing_bid = book.inv[coin] < 0
        reducing_ask = book.inv[coin] > 0
        if bid_usd > 0 and bid_usd < HL_MIN_ORDER_USD and not reducing_bid:
            bid_usd = 0.0
        if ask_usd > 0 and ask_usd < HL_MIN_ORDER_USD and not reducing_ask:
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
        live = next((q for q in (book.quotes.get(coin) or []) if q.retire_at is None), None)
        # only post_fill may keep the live quote: a timer/jump/pause must still replace
        if reason == "post_fill" and live is not None and (bid_sz > 0 or ask_sz > 0):
            if abs(live.bid_sz - bid_sz) <= 1e-12 and abs(live.ask_sz - ask_sz) <= 1e-12:
                return
        if bid_sz <= 0 and ask_sz <= 0:
            had_live = any(q.retire_at is None for q in (book.quotes.get(coin) or []))
            if had_live:
                for q in (book.quotes.get(coin) or []):
                    if q.retire_at is None:
                        q.retire_at = now + LATENCY_API_S
                book.n_replaces[coin] = book.n_replaces.get(coin, 0) + 1
                _bump_reason(book, coin, reason)
            book.intentionally_flat[coin] = True
            book.last_refresh[coin] = now
            return

        book.intentionally_flat[coin] = False
        for q in (book.quotes.get(coin) or []):
            if q.retire_at is None:
                q.retire_at = now + LATENCY_API_S
        book.n_replaces[coin] = book.n_replaces.get(coin, 0) + 1
        _bump_reason(book, coin, reason)

        bids, asks = self.books_l2.get(coin, ([], []))
        vis_bids = bids[:BOOK_DEPTH_FOR_QUEUE]
        vis_asks = asks[:BOOK_DEPTH_FOR_QUEUE]
        b_better, b_same = _queue_at_price(vis_bids, bid_px, "bid") if bid_sz else (0.0, 0.0)
        a_better, a_same = _queue_at_price(vis_asks, ask_px, "ask") if ask_sz else (0.0, 0.0)
        nxt = Quote(
            bid_px=bid_px, ask_px=ask_px, bid_sz=bid_sz, ask_sz=ask_sz,
            placed_at=now, effective_at=now + LATENCY_API_S,
            bid_queue_ahead=b_better + b_same,
            ask_queue_ahead=a_better + a_same,
            bid_sz_opt=bid_sz, ask_sz_opt=ask_sz,
            retire_at=None, ref_mid=mid,
            bid_sz_tt=bid_sz, ask_sz_tt=ask_sz,
            beyond_visible_bid=_beyond_visible(vis_bids, bid_px, "bid") if bid_sz else False,
            beyond_visible_ask=_beyond_visible(vis_asks, ask_px, "ask") if ask_sz else False,
        )
        book.quotes.setdefault(coin, []).append(nxt)
        book.n_quotes_placed[coin] = book.n_quotes_placed.get(coin, 0) + 1
        if nxt.beyond_visible_bid or nxt.beyond_visible_ask:
            book.n_quotes_beyond[coin] = book.n_quotes_beyond.get(coin, 0) + 1
        book.last_refresh[coin] = now

    def maybe_refresh(self, now: float, force: bool = False) -> None:
        for book in self.books.values():
            for coin in HL_COINS:
                mid = self.mids.get(coin)
                if mid is None:
                    continue
                self._sweep_expired(book, coin, now)
                q = self._current_quote(book, coin)
                last = book.last_refresh.get(coin, 0.0)
                flat = book.intentionally_flat.get(coin, False)
                reasons = []
                if force:
                    reasons.append("timer")
                if q is None and not flat:
                    reasons.append("timer")
                if (now - last) >= book.refresh_s:
                    reasons.append("timer")
                if q is not None and mid > 0:
                    ref = q.ref_mid if q.ref_mid else (q.bid_px + q.ask_px) / 2.0
                    if ref > 0 and abs(mid - ref) / mid > MID_JUMP_TRIGGER * book.spread:
                        reasons.append("jump")
                        if abs(mid - ref) / mid > 3 * book.spread:
                            if not book.coin_paused(coin, now):
                                book.paused_until[coin] = now + max(5.0, book.refresh_s)
                                book.n_pauses[coin] = book.n_pauses.get(coin, 0) + 1
                                self._retire_quote(book, coin, now)
                until = book.paused_until.get(coin, 0.0)
                if until and last < until <= now and q is None:
                    reasons.append("pause_resume")
                sides = self._allowed_sides(book, coin)
                prev = book.allowed_sides.get(coin)
                if prev is not None and sides != prev:
                    reasons.append("sides_edge")
                book.allowed_sides[coin] = sides
                if book.coin_paused(coin, now):
                    self._retire_quote(book, coin, now)
                elif reasons:
                    self._place(book, coin, now, reason=_primary_reason(reasons))

    def _try_fill(self, book, coin, px, sz, side, now) -> None:
        qs = self._fillable_quotes(book, coin, now)
        if not qs:
            return
        n_before = book.n_fills
        for q in qs:
            self._try_fill_one(book, coin, px, sz, side, now, q)
        if book.n_fills > n_before:
            for c in HL_COINS:
                if not book.coin_paused(c, now):
                    self._place(book, c, now, reason="post_fill")

    def _clamp_reduce(self, book, coin, side, sz) -> float:
        pos = book.inv.get(coin, 0.0)
        if side == "buy" and pos < 0:
            return min(sz, -pos)
        if side == "sell" and pos > 0:
            return min(sz, pos)
        return sz

    def _try_fill_one(self, book, coin, px, sz, side, now, q) -> None:
        # optimistic (no queue); kept as a count, not PnL
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
        # paper path: a strict trade-through zeros the queue and fills the rest
        if side == "A" and q.bid_sz > 0:
            through = px < q.bid_px - 1e-12
            at_or_through = px <= q.bid_px + 1e-9
            if through:
                q.bid_queue_ahead = 0.0
                fill_sz = self._clamp_reduce(book, coin, "buy", q.bid_sz)
                if fill_sz > 0:
                    self._apply_fill(book, coin, "buy", q.bid_px, fill_sz, now, through=True, beyond_visible=q.beyond_visible_bid)
                    q.bid_sz -= fill_sz
                    if q.bid_sz <= 1e-12:
                        q.bid_sz = 0.0
            elif at_or_through:
                remain = sz
                if q.bid_queue_ahead > 0:
                    eat = min(q.bid_queue_ahead, remain)
                    q.bid_queue_ahead -= eat
                    remain -= eat
                if remain > 0 and q.bid_queue_ahead <= 1e-15:
                    fill_sz = self._clamp_reduce(book, coin, "buy", min(q.bid_sz, remain))
                    if fill_sz > 0:
                        self._apply_fill(book, coin, "buy", q.bid_px, fill_sz, now, through=False, beyond_visible=q.beyond_visible_bid)
                        q.bid_sz -= fill_sz
                        if q.bid_sz <= 1e-12:
                            q.bid_sz = 0.0
            elif px > q.bid_px:
                if q.bid_queue_ahead > 0:
                    q.bid_queue_ahead = max(0.0, q.bid_queue_ahead - sz)
        if side == "B" and q.ask_sz > 0:
            through = px > q.ask_px + 1e-12
            at_or_through = px >= q.ask_px - 1e-9
            if through:
                q.ask_queue_ahead = 0.0
                fill_sz = self._clamp_reduce(book, coin, "sell", q.ask_sz)
                if fill_sz > 0:
                    self._apply_fill(book, coin, "sell", q.ask_px, fill_sz, now, through=True, beyond_visible=q.beyond_visible_ask)
                    q.ask_sz -= fill_sz
                    if q.ask_sz <= 1e-12:
                        q.ask_sz = 0.0
            elif at_or_through:
                remain = sz
                if q.ask_queue_ahead > 0:
                    eat = min(q.ask_queue_ahead, remain)
                    q.ask_queue_ahead -= eat
                    remain -= eat
                if remain > 0 and q.ask_queue_ahead <= 1e-15:
                    fill_sz = self._clamp_reduce(book, coin, "sell", min(q.ask_sz, remain))
                    if fill_sz > 0:
                        self._apply_fill(book, coin, "sell", q.ask_px, fill_sz, now, through=False, beyond_visible=q.beyond_visible_ask)
                        q.ask_sz -= fill_sz
                        if q.ask_sz <= 1e-12:
                            q.ask_sz = 0.0
            elif px < q.ask_px:
                if q.ask_queue_ahead > 0:
                    q.ask_queue_ahead = max(0.0, q.ask_queue_ahead - sz)

    def _projected_gross(self, book, coin, side, sz, px) -> float:
        notionals = {}
        for c, q in book.inv.items():
            m = self.mids.get(c)
            if m is not None:
                notionals[c] = q * m
        delta_qty = sz if side == "buy" else -sz
        notionals[coin] = (book.inv.get(coin, 0.0) + delta_qty) * px
        return sum(abs(v) for v in notionals.values())

    def _would_respect_cap(self, book, coin, side, sz, px) -> bool:
        projected = self._projected_gross(book, coin, side, sz, px)
        current = book.inv_usd_gross(self.mids)
        if projected <= current * 1.0000001:
            return True
        return projected <= book.cap(self.mids) * 1.001

    def _mid_before(self, coin: str, t: float):
        buf = self.mid_hist.get(coin)
        if not buf:
            return None
        last = None
        for ts, mid in buf:
            if ts < t:
                last = mid
            else:
                break
        return last

    def _mid_at(self, coin: str, t: float):
        buf = self.mid_hist.get(coin)
        if not buf:
            return None
        if t < buf[0][0]:
            return None
        last = None
        last_ts = None
        next_ts = None
        for ts, mid in buf:
            if ts <= t:
                last = mid
                last_ts = ts
            else:
                next_ts = ts
                break
        if last is None:
            return None
        if next_ts is not None and last_ts is not None and (next_ts - last_ts) > MID_GAP_S:
            return None
        if next_ts is None and last_ts is not None and (t - last_ts) > MID_GAP_S:
            return None
        return last

    def _apply_fill(self, book, coin, our_side, px, sz, now, queue: bool = True, through: bool = False, beyond_visible: bool = False) -> None:
        gross_before = book.inv_usd_gross(self.mids) if self.mids else 0.0
        ntl = px * sz
        fee = ntl * HL_MAKER_FEE
        fee2 = ntl * HL_MAKER_FEE_2X
        old = book.inv[coin]
        avg = book.avg_entry.get(coin, 0.0)
        if our_side == "buy":
            book.usdc -= ntl + fee
            new = old + sz
            if abs(new) < 1e-12:
                new = 0.0
            if old < 0:
                cover = min(sz, -old)
                book.realized_pnl += (avg - px) * cover
                if new < 0:
                    pass
                elif new == 0:
                    avg = 0.0
                else:
                    avg = px
            elif old == 0:
                avg = px
            else:
                avg = (avg * old + px * sz) / new
            book.n_buys += 1
        else:
            book.usdc += ntl - fee
            new = old - sz
            if abs(new) < 1e-12:
                new = 0.0
            if old > 0:
                close = min(sz, old)
                book.realized_pnl += (px - avg) * close
                if new > 0:
                    pass
                elif new == 0:
                    avg = 0.0
                else:
                    avg = px
            elif old == 0:
                avg = px
            else:
                avg = (avg * old + px * (new - old)) / new
            book.n_sells += 1
        book.inv[coin] = new
        book.avg_entry[coin] = avg
        book.fees_paid += fee
        book.fees_paid_2x += fee2
        book.n_fills += 1
        book.volume_usd += ntl
        self._fill_seq += 1
        fid = str(self._fill_seq)
        mid = self._mid_before(coin, now)
        if mid is None:
            mid = self.mids.get(coin, px)
            book.n_mid_before_fallback += 1
        book.pending_as.append(
            PendingAS(
                fill_id=fid, coin=coin, t=now, mid_at_fill=mid, side=our_side,
                notional=ntl, through=through,
            )
        )
        if through:
            book.n_fills_through += 1
            book.volume_through += ntl
            book.n_fills_tt = book.n_fills_through
            book.volume_tt = book.volume_through
        cap = book.cap(self.mids) if self.mids else INITIAL_EQUITY
        gross_after = book.inv_usd_gross(self.mids) if self.mids else 0.0
        if gross_after > gross_before + 1e-12 and gross_after > cap + 1e-9:
            over = gross_after - cap
            book.n_cap_overshoot += 1
            book.max_cap_overshoot = max(book.max_cap_overshoot, over)
        append_jsonl(FILLS_PATH, json.dumps({
            "id": fid, "t": now, "arm": book.arm, "mode": book.mode,
            "spread": book.spread, "refresh_s": book.refresh_s, "coin": coin,
            "side": our_side, "px": px, "sz": sz, "notional": ntl, "fee": fee,
            "mid": mid, "inv_after": book.inv[coin], "latency_s": LATENCY_API_S,
            "queue_aware": queue, "through": through,
            "beyond_visible": beyond_visible, "cap": book.cap(self.mids),
            "equity": book.equity(self.mids),
            "git_rev": read_git_rev(),
        }))

    def _sample_as(self, book, pending, now) -> None:
        still = []
        for p in pending:
            sign = 1.0 if p.side == "buy" else -1.0
            for h, tag in ((1, "1s"), (5, "5s"), (30, "30s"), (60, "1m"), (300, "5m")):
                done = f"done_{tag}"
                if getattr(p, done):
                    continue
                if now < p.t + h:
                    continue
                tol = AS_TOLERANCE_S.get(tag, 0.0)
                mid_h = self._mid_at(p.coin, p.t + h)
                if mid_h is None:
                    buf = self.mid_hist.get(p.coin)
                    coin_now = buf[-1][0] if buf else now
                    if not buf:
                        if coin_now > p.t + h + tol:
                            setattr(p, done, True)
                            book.as_seen[tag] = book.as_seen.get(tag, 0) + 1
                            book.as_discard[tag] = book.as_discard.get(tag, 0) + 1
                            continue
                        mid_h = self.mids.get(p.coin)
                        if mid_h is None:
                            continue
                    elif coin_now > p.t + h + tol:
                        setattr(p, done, True)
                        book.as_seen[tag] = book.as_seen.get(tag, 0) + 1
                        book.as_discard[tag] = book.as_discard.get(tag, 0) + 1
                        continue
                    else:
                        continue
                setattr(p, done, True)
                book.as_seen[tag] = book.as_seen.get(tag, 0) + 1
                move = (mid_h - p.mid_at_fill) / p.mid_at_fill if p.mid_at_fill else 0.0
                adv = -sign * move
                weighted = adv * p.notional
                setattr(book, f"as_{tag}_sum", getattr(book, f"as_{tag}_sum") + weighted)
                setattr(book, f"as_{tag}_ntl", getattr(book, f"as_{tag}_ntl") + p.notional)
                book.as_coin_sum.setdefault(p.coin, {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")})
                book.as_coin_ntl.setdefault(p.coin, {k: 0.0 for k in ("1s", "5s", "30s", "1m", "5m")})
                book.as_coin_sum[p.coin][tag] = book.as_coin_sum[p.coin].get(tag, 0.0) + weighted
                book.as_coin_ntl[p.coin][tag] = book.as_coin_ntl[p.coin].get(tag, 0.0) + p.notional
                if getattr(p, "through", False):
                    book.as_through_sum[tag] = book.as_through_sum.get(tag, 0.0) + weighted
                    book.as_through_ntl[tag] = book.as_through_ntl.get(tag, 0.0) + p.notional
                    book.as_tt_sum[tag] = book.as_through_sum[tag]
                    book.as_tt_ntl[tag] = book.as_through_ntl[tag]
            if not (p.done_1s and p.done_5s and p.done_30s and p.done_1m and p.done_5m):
                still.append(p)
        book.pending_as = still

    def _resolve_as(self, now: float) -> None:
        for book in self.books.values():
            self._sample_as(book, book.pending_as, now)

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
                b.spread = max(RECAL_MIN_SPREAD, min(MAX_SPREAD, b.spread + ds))
                if new_refresh is not None:
                    dr = max(-MAX_REFRESH_STEP_S, min(MAX_REFRESH_STEP_S, new_refresh - b.refresh_s))
                    b.refresh_s = max(MIN_REFRESH_S, min(MAX_REFRESH_S, b.refresh_s + dr))
                b.last_recal_at = now
                b.n_recals += 1
            self.store.insert_event(
                now, "recal", json.dumps({
                    "recal_key": key,
                    "target": target,
                    "n_promissor": getattr(self, "_last_n_promissor", None),
                    "n_by_venue": getattr(self, "_last_n_by_venue", None),
                    "median_by_venue": getattr(self, "_last_median_by_venue", None),
                })
            )
            self.store.commit()

    def _fit_from_observer(self, since: float):
        """Median of PROMISSOR makers over the compact 14d hist (fallback: latest)."""
        hist = []
        try:
            hist = self.store.promissor_hist(since)
        except Exception:
            hist = []
        if hist:
            groups = {}
            for m in hist:
                gkey = (m.get("venue"), m.get("maker_id"), m.get("coin"))
                groups.setdefault(gkey, []).append(m)
            makers = []
            for rows in groups.values():
                nears = [r["median_near_bps"] for r in rows if r.get("median_near_bps") is not None]
                cycles = [
                    r["median_cycle_s"] for r in rows
                    if r.get("median_cycle_s") is not None and r.get("cycle_usable")
                ]
                sample = dict(rows[-1])
                sample["median_near_bps"] = statistics.median(nears) if nears else None
                sample["median_cycle_s"] = statistics.median(cycles) if cycles else None
                sample["cycle_usable"] = bool(cycles)
                makers.append(sample)
        else:
            makers = self.store.promissor_makers(since)
        # one vote per (venue, maker_id): a maker quoting both coins does not
        # count twice even when near/cycle differ.
        by_acct = {}
        for m in makers:
            by_acct.setdefault((m.get("venue"), m.get("maker_id")), []).append(m)
        uniq = []
        for rows in by_acct.values():
            nears = [r["median_near_bps"] for r in rows if r.get("median_near_bps") is not None]
            cycles = []
            for r in rows:
                cyc = r.get("median_cycle_s")
                if cyc is None:
                    continue
                if r.get("venue") == "hl" and not r.get("cycle_usable"):
                    continue
                cycles.append(cyc)
            sample = dict(rows[0])
            sample["median_near_bps"] = statistics.median(nears) if nears else None
            sample["median_cycle_s"] = statistics.median(cycles) if cycles else None
            sample["cycle_usable"] = bool(cycles)
            uniq.append(sample)
        makers = uniq
        self._last_n_promissor = len(makers)
        n_by_venue = {}
        median_by_venue = {}
        for m in makers:
            v = m.get("venue") or "unknown"
            n_by_venue[v] = n_by_venue.get(v, 0) + 1
        for v in n_by_venue:
            v_spreads = [
                m["median_near_bps"] / 1e4
                for m in makers
                if (m.get("venue") or "unknown") == v and m.get("median_near_bps") is not None
            ]
            v_cycles = []
            for m in makers:
                if (m.get("venue") or "unknown") != v:
                    continue
                cyc = m.get("median_cycle_s")
                if cyc is None:
                    continue
                if v == "hl" and not m.get("cycle_usable"):
                    continue
                v_cycles.append(cyc)
            median_by_venue[v] = {
                "n": n_by_venue[v],
                "spread": statistics.median(v_spreads) if v_spreads else None,
                "refresh_s": statistics.median(v_cycles) if v_cycles else None,
            }
        self._last_n_by_venue = n_by_venue
        self._last_median_by_venue = median_by_venue
        if not makers:
            return None  # no param change if nobody passes the filter
        spreads = [m["median_near_bps"] / 1e4 for m in makers if m.get("median_near_bps") is not None]
        cycles = []
        for m in makers:
            cyc = m.get("median_cycle_s")
            if cyc is None:
                continue
            # Lighter cycles are usable; HL only when not censored by the poll gap.
            if m.get("venue") == "hl" and not m.get("cycle_usable"):
                continue
            cycles.append(cyc)
        if not spreads:
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
        spread = max(RECAL_MIN_SPREAD, statistics.median(spreads) * vol_scale)
        refresh = max(3.0, statistics.median(cycles)) if cycles else None
        return spread, refresh
