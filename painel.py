#!/usr/bin/env python3
"""mm-painel — denser read-only TUI for mm-lab ($100 1x). Quit with q."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

ROOT = Path(os.environ.get("MM_LAB_ROOT", Path(__file__).resolve().parent))
DATA = ROOT / "data"
STATE = DATA / "sim_state.json"
HB = DATA / "heartbeat"
PID = DATA / "lab.pid"
SUP_PID = DATA / "supervisor.pid"
DB = DATA / "observer.sqlite"
DASH = DATA / "dashboard.json"
PNL_HIST = DATA / "pnl_hist.jsonl"

REFRESH_S = 1.0
VIABLE_MIN_DIST_BPS = 10.0
VIABLE_MAX_DIST_BPS = 100.0
VIABLE_MIN_CYCLE_S = 3.0
VIABLE_MIN_SIZE_USD = 50.0
TZ = ZoneInfo("America/Sao_Paulo")


def _now_brt() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S America/Sao_Paulo")


def _alive(pid_path: Path, hb_path: Path, max_age: float = 90.0) -> Tuple[bool, str, Optional[int]]:
    if not pid_path.exists():
        return False, "sem pid", None
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)
    except Exception as e:
        return False, f"morto ({e})", None
    age = None
    if hb_path.exists():
        try:
            age = time.time() - float(hb_path.read_text().strip())
        except Exception:
            age = None
    if age is not None and age > max_age:
        return False, f"hb {age:.0f}s", pid
    return True, f"pid={pid}" + (f" hb={age:.0f}s" if age is not None else ""), pid


def _rss_mb(pid: Optional[int]) -> Optional[float]:
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def _cpu_pct(pid: Optional[int], cache: Dict[str, Any]) -> Optional[float]:
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        ut = int(parts[13]) + int(parts[14])
        now = time.time()
        prev = cache.get("cpu")
        cache["cpu"] = (now, ut)
        if not prev:
            return None
        dt = now - prev[0]
        if dt <= 0:
            return None
        return max(0.0, (ut - prev[1]) / dt / os.sysconf(os.sysconf_names["SC_CLK_TCK"]) * 100.0)
    except Exception:
        return None


def _meminfo() -> Tuple[float, float]:
    total = avail = 0.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) / 1024.0
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return total, avail


def _disk_free_gb(path: Path) -> float:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / (1024 ** 3)
    except Exception:
        return 0.0


def _spark(values: List[float], width: int = 24) -> str:
    if not values:
        return "·" * min(width, 8)
    blocks = "▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    if mx - mn < 1e-6:
        return "▄" * min(width, max(1, len(values)))  # flat line
    span = mx - mn
    if len(values) > width:
        step = len(values) / width
        vals = [values[int(i * step)] for i in range(width)]
    else:
        vals = values
    return "".join(blocks[min(7, int((v - mn) / span * 7))] for v in vals)


def _fmt_bps(x: Optional[float]) -> str:
    return f"{x:.1f}" if x is not None else "—"


def _load_pnl_hist(n: int = 90) -> Dict[str, List[float]]:
    out: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=n))
    if not PNL_HIST.exists():
        return {}
    try:
        lines = PNL_HIST.read_text().splitlines()[-n:]
        for line in lines:
            row = json.loads(line)
            for k, v in row.items():
                if k == "t":
                    continue
                try:
                    out[k].append(float(v))
                except Exception:
                    pass
    except Exception:
        pass
    return {k: list(v) for k, v in out.items()}


def _is_viable(m: Dict[str, Any]) -> bool:
    near, cycle, size = m.get("near"), m.get("cycle"), m.get("size")
    if not m.get("viable") or near is None or cycle is None:
        return False
    if not (VIABLE_MIN_DIST_BPS <= near <= VIABLE_MAX_DIST_BPS):
        return False
    if cycle < VIABLE_MIN_CYCLE_S:
        return False
    if size is not None and size < VIABLE_MIN_SIZE_USD:
        return False
    return True


def _load_makers(limit: int = 40) -> List[Dict[str, Any]]:
    if not DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """SELECT venue, maker_id, coin, median_cycle_s, median_near_bps,
                          median_size_usd, pause_frac, est_pnl, maker_fills, viable, n_snaps,
                          COALESCE(promissor,0), COALESCE(pnl_known,0)
                   FROM maker_stats
                   ORDER BY COALESCE(promissor,0) DESC, viable DESC, n_snaps DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            has_prom = True
        except sqlite3.OperationalError:
            rows = con.execute(
                """SELECT venue, maker_id, coin, median_cycle_s, median_near_bps,
                          median_size_usd, pause_frac, est_pnl, maker_fills, viable, n_snaps
                   FROM maker_stats ORDER BY viable DESC, n_snaps DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            has_prom = False
        layers: Dict[Tuple[str, str, str], int] = {}
        for venue, mid, coin, *_ in rows:
            r = con.execute(
                """SELECT n_bid, n_ask FROM maker_snap
                   WHERE venue=? AND maker_id=? AND coin=? ORDER BY t DESC LIMIT 1""",
                (venue, mid, coin),
            ).fetchone()
            if r:
                layers[(venue, mid, coin)] = (r[0] or 0) + (r[1] or 0)
        con.close()
        out = []
        for r in rows:
            if has_prom:
                venue, mid, coin, cycle, near, size, pause, pnl, fills, viable, snaps, prom, pk = r
            else:
                venue, mid, coin, cycle, near, size, pause, pnl, fills, viable, snaps = r
                prom, pk = 0, 0
            out.append({
                "venue": venue, "maker_id": mid, "coin": coin,
                "cycle": cycle, "near": near, "size": size, "pause": pause,
                "pnl": pnl, "fills": fills, "viable": viable, "snaps": snaps,
                "layers": layers.get((venue, mid, coin)),
                "promissor": bool(prom), "pnl_known": bool(pk),
            })
        return out
    except Exception:
        return []


def _load_market() -> List[Dict[str, Any]]:
    if not DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = con.execute(
            """SELECT venue, coin, mid, imbalance, rvol_1m, volume_1m, funding, t
               FROM market_state
               WHERE t > (SELECT MAX(t) FROM market_state) - 30
               ORDER BY t DESC"""
        ).fetchall()
        con.close()
        seen = set()
        out = []
        for r in rows:
            key = (r[0], r[1])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "venue": r[0], "coin": r[1], "mid": r[2], "imb": r[3],
                "rvol": r[4], "vol": r[5], "funding": r[6], "t": r[7],
            })
        return out
    except Exception:
        return []


def _short_acct(mid: str) -> str:
    if len(mid) <= 10:
        return mid
    if mid.startswith("0x"):
        return mid[:6] + "…" + mid[-4:]
    return mid[-8:]


def _arm_as(b: Dict[str, Any], tag: str) -> Optional[float]:
    if b.get(f"as_{tag}_ntl"):
        return b[f"as_{tag}_sum"] / b[f"as_{tag}_ntl"] * 1e4
    return None


def _arm_as1m(b: Dict[str, Any]) -> Optional[float]:
    if b.get("as_1m_ntl"):
        return b["as_1m_sum"] / b["as_1m_ntl"] * 1e4
    return None


def build_view(cpu_cache: Dict[str, Any]) -> Layout:
    lab_ok, lab_msg, lab_pid = _alive(PID, HB)
    sup_ok = False
    if SUP_PID.exists():
        try:
            os.kill(int(SUP_PID.read_text().strip()), 0)
            sup_ok = True
        except Exception:
            pass

    state: Dict[str, Any] = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            state = {}
    dash: Dict[str, Any] = {}
    if DASH.exists():
        try:
            dash = json.loads(DASH.read_text())
        except Exception:
            pass

    rss = _rss_mb(lab_pid)
    cpu = _cpu_pct(lab_pid, cpu_cache)
    mem_tot, mem_av = _meminfo()
    disk = _disk_free_gb(ROOT)
    started = state.get("started_at") or time.time()
    uptime_h = (time.time() - started) / 3600.0
    saved_age = time.time() - state.get("saved_at", 0) if state.get("saved_at") else None
    pnl_hist = _load_pnl_hist()
    books = state.get("books") or {}
    obs = dash.get("observer") or {}
    errs = obs.get("api_errors_recent") or []

    # --- header ---
    title = Text()
    title.append(" mm-lab ", style="bold white on dark_blue")
    title.append(" painel ", style="bold cyan")
    title.append(f" {_now_brt()} ", style="dim")
    title.append(" q=sair ", style="bold yellow")

    # --- health strip (no SPOT) ---
    health = Text()
    health.append("LAB ", style="bold")
    health.append("UP " if lab_ok else "DOWN ", style="bold green" if lab_ok else "bold red")
    health.append(lab_msg + "  ", style="dim")
    health.append(f"sup={'ok' if sup_ok else 'off'}  ", style="dim")
    health.append(f"up={uptime_h:.2f}h  ")
    if rss is not None:
        health.append(f"RSS={rss:.0f}MB  ", style="green" if rss < 150 else "red")
    else:
        health.append("RSS=?  ")
    health.append(f"CPU={cpu:.1f}%  " if cpu is not None else "CPU=?  ")
    health.append(f"RAM {mem_av:.0f}/{mem_tot:.0f}MB  ")
    health.append(f"disk {disk:.0f}GB  ")
    if saved_age is not None:
        health.append(f"age={saved_age:.0f}s  ", style="dim")
    health.append(
        f"obs L={obs.get('lighter_makers','?')} HL={obs.get('hl_makers','?')} "
        f"viable={obs.get('viable_count','?')} ★={obs.get('promissor_count','?')}  ",
        style="dim",
    )
    if errs:
        health.append("err:" + str(errs[-1])[:48], style="bold red")
    else:
        health.append("err:none", style="dim")

    # --- extras strip: fills/h, viable by venue, A FIX vs CMP, next B recal ---
    extras = Text()
    fills_tot = sum(int(b.get("n_fills") or 0) for b in books.values())
    fills_h = fills_tot / uptime_h if uptime_h > 0.01 else 0.0
    extras.append(f"fills/h≈{fills_h:.0f}  ", style="cyan")
    makers_all = _load_makers()
    viable_list = [m for m in makers_all if _is_viable(m)]
    prom_list = [m for m in makers_all if m.get("promissor")]
    v_lig = sum(1 for m in viable_list if m["venue"] == "lighter")
    v_hl = sum(1 for m in viable_list if m["venue"] == "hl")
    p_lig = sum(1 for m in prom_list if m["venue"] == "lighter")
    p_hl = sum(1 for m in prom_list if m["venue"] == "hl")
    extras.append(f"★prom lig={p_lig} hl={p_hl}  ", style="bold green")
    extras.append(f"viable lig={v_lig} hl={v_hl}  ", style="yellow")
    # next B recal (hours)
    b_fix = books.get("B_FIX") or {}
    nxt_b = b_fix.get("next_recal_s")
    if nxt_b is not None:
        extras.append(f"B recal {nxt_b/3600:.1f}h  ", style="yellow")
    a_fix_sp = _spark(pnl_hist.get("A_FIX", []), 14)
    a_cmp_sp = _spark(pnl_hist.get("A_CMP", []), 14)
    extras.append("A ", style="bold")
    extras.append("FIX ", style="cyan")
    extras.append(a_fix_sp + "  ", style="cyan")
    extras.append("CMP ", style="magenta")
    extras.append(a_cmp_sp, style="magenta")
    # last recal params if any arm has n_recals>0
    recal_bits = []
    for k in ("B_FIX", "B_CMP", "C_FIX", "D_FIX"):
        b = books.get(k) or {}
        if (b.get("n_recals") or 0) > 0:
            recal_bits.append(
                f"{k}:spr={b.get('spread',0)*1e4:.0f}b/ref={b.get('refresh_s',0):.0f}s"
            )
    if recal_bits:
        extras.append("  |  " + " · ".join(recal_bits[:3]), style="dim")

    # --- arms table (dense) ---
    arms = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    arms.add_column("Arm", style="bold", no_wrap=True, min_width=7)
    arms.add_column("Mode", no_wrap=True)
    arms.add_column("Spr", justify="right")
    arms.add_column("Equity", justify="right")
    arms.add_column("Ret%", justify="right")
    arms.add_column("Cap", justify="right")
    arms.add_column("Inv%", justify="right")
    arms.add_column("F/O", justify="right", no_wrap=True, min_width=7)
    arms.add_column("AS5s", justify="right")
    arms.add_column("AS1m", justify="right")
    arms.add_column("Fund", justify="right")
    arms.add_column("Recal", justify="right")
    arms.add_column("Spark", no_wrap=True)

    def _arm_sort(k: str):
        b = books[k]
        neigh = 1 if b.get("neighbor") else 0
        rk = b.get("recal_key") or k.split("_")[0]
        mode = b.get("mode") or ""
        spr = b.get("spread") or 0
        return (neigh, "ABCD".find(rk[0]) if rk and rk[0] in "ABCD" else 9, spr, 0 if mode == "FIX" else 1, k)

    order = sorted(books.keys(), key=_arm_sort)
    rets = [b.get("ret_pct") for b in books.values() if b.get("ret_pct") is not None]
    best_ret = max(rets) if rets else None

    for key in order:
        b = books[key]
        mode = b.get("mode") or "?"
        mode_style = "cyan" if mode == "FIX" else "magenta"
        ret = b.get("ret_pct")
        is_best = best_ret is not None and ret is not None and ret == best_ret and len(rets) > 1
        if ret is None:
            ret_t = Text("—", style="dim")
        else:
            st = "bold green on dark_green" if is_best else ("green" if ret >= 0 else "red")
            ret_t = Text(f"{ret:+.2f}", style=st)
            if is_best:
                ret_t.append("★", style="bold yellow")
        invp = b.get("inv_cap_pct")
        inv_s = f"{invp:.0f}" if invp is not None else "—"
        nxt = b.get("next_recal_s")
        if nxt is None:
            recal = "fix" if (b.get("recal_key") == "A" or b.get("neighbor")) else "—"
        else:
            recal = f"{nxt/3600:.1f}h"
        nf = int(b.get("n_fills") or 0)
        no = int(b.get("n_fills_opt") or 0)
        fund = b.get("funding_pnl")
        fund_s = f"${fund:+.3f}" if fund is not None else "—"
        spark = _spark(pnl_hist.get(key, []), 12)
        name_style = "dim" if b.get("neighbor") else ("bold yellow" if is_best else "bold")
        arms.add_row(
            Text(key, style=name_style),
            Text(mode, style=mode_style),
            f"{b.get('spread', 0) * 1e4:.0f}b",
            f"${b.get('equity', 0):.2f}",
            ret_t,
            f"${b.get('cap', 0):.0f}",
            inv_s,
            f"{nf}/{no}",
            _fmt_bps(_arm_as(b, "5s")),
            _fmt_bps(_arm_as1m(b)),
            fund_s,
            recal,
            Text(spark, style="cyan"),
        )

    # --- makers: promissors first, then viable-unranked ---
    makers_t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    makers_t.add_column("★", no_wrap=True)
    makers_t.add_column("V", no_wrap=True)
    makers_t.add_column("Acct", no_wrap=True)
    makers_t.add_column("C", no_wrap=True)
    makers_t.add_column("Cyc", justify="right")
    makers_t.add_column("Near", justify="right")
    makers_t.add_column("Lyr", justify="right")
    makers_t.add_column("PnL", justify="right")

    def _maker_rank(m: Dict[str, Any]) -> Tuple:
        prom = 0 if m.get("promissor") else 1
        ok = 0 if _is_viable(m) else 2
        near = m.get("near") or 999
        cycle = m.get("cycle") or 999
        pnl = -(m.get("pnl") or 0) if m.get("pnl_known") else 0
        return (prom, ok, pnl, abs(near - 20.0), cycle)

    # account-level ★: HL account with known PnL>0 stars every viable market row
    acct_pos = {
        (m["venue"], m["maker_id"])
        for m in makers_all
        if m.get("pnl_known") and (m.get("pnl") or 0) > 0
    }
    for m in makers_all:
        if (m["venue"], m["maker_id"]) in acct_pos and _is_viable(m):
            m["promissor"] = True
    ranked = sorted(makers_all, key=_maker_rank)
    pnl_shown = set()
    shown = 0
    n_prom_show = 0
    n_viable_only = 0
    for m in ranked:
        if shown >= 12:
            break
        viable = _is_viable(m)
        prom = bool(m.get("promissor"))
        if not viable and not prom:
            continue
        if prom:
            tag = Text("★", style="bold yellow")
            row_style = "bold green"
            n_prom_show += 1
            if m.get("pnl_known"):
                ak = (m["venue"], m["maker_id"])
                if ak in pnl_shown:
                    pnl_s = "acct↑"  # same account PnL already shown above
                else:
                    pnl_s = f"{(m['pnl'] or 0):+.0f}"
                    pnl_shown.add(ak)
            else:
                pnl_s = "proxy"
        else:
            tag = Text("·", style="dim")
            row_style = "yellow"
            n_viable_only += 1
            pnl_s = "?" if not m.get("pnl_known") else f"{(m['pnl'] or 0):+.0f}"
        makers_t.add_row(
            tag,
            m["venue"][:3],
            _short_acct(m["maker_id"]),
            m["coin"],
            f"{m['cycle']:.1f}" if m["cycle"] is not None else "—",
            f"{m['near']:.0f}" if m["near"] is not None else "—",
            str(m["layers"] if m["layers"] is not None else "—"),
            pnl_s,
            style=row_style,
        )
        shown += 1
    if shown == 0:
        makers_t.add_row("—", "—", "sem promissor/viable", "—", "—", "—", "—", "—", style="dim")

    # --- market strip ---
    mkt = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    mkt.add_column("Ven", no_wrap=True)
    mkt.add_column("Coin", no_wrap=True)
    mkt.add_column("Mid", justify="right")
    mkt.add_column("Imb", justify="right")
    mkt.add_column("rVol", justify="right")
    mkt.add_column("Vol1m$", justify="right")
    mkt.add_column("Fund", justify="right")
    # prefer HL then Lighter, BTC then HYPE
    order_m = sorted(
        _load_market(),
        key=lambda r: (0 if r["venue"] == "hl" else 1, 0 if r["coin"] == "BTC" else 1),
    )
    for r in order_m:
        imb = r["imb"]
        imb_s = f"{imb:+.2f}" if imb is not None else "—"
        imb_style = "green" if (imb or 0) > 0.1 else ("red" if (imb or 0) < -0.1 else "")
        mid = r["mid"]
        if mid and mid >= 1000:
            mid_s = f"{mid:,.1f}"
        elif mid:
            mid_s = f"{mid:.3f}"
        else:
            mid_s = "—"
        mkt.add_row(
            r["venue"][:3],
            r["coin"],
            mid_s,
            Text(imb_s, style=imb_style),
            f"{(r['rvol'] or 0) * 1e4:.2f}b" if r["rvol"] is not None else "—",
            f"{(r['vol'] or 0):,.0f}",
            f"{(r['funding'] or 0) * 100:.4f}%" if r["funding"] is not None else "—",
        )

    layout = Layout()
    layout.split_column(
        Layout(Align.left(title), name="head", size=1),
        Layout(Panel(health, title="[bold]Health[/]", border_style="magenta", padding=(0, 1)), name="health", size=3),
        Layout(Panel(extras, title="[bold]Extras[/] fills/h · ★promissor · viable · A FIX/CMP · B recal", border_style="bright_black", padding=(0, 1)), name="extras", size=3),
        Layout(name="main", size=18),
        Layout(Panel(mkt, title="[bold]Mercado[/] BTC/HYPE · HL & Lighter", border_style="yellow", padding=(0, 1)), name="mkt", size=8),
    )
    layout["main"].split_row(
        Layout(
            Panel(
                arms,
                title="[bold]Braços $100 1x[/] FIX=cap$100 · CMP=cap=equity · ★=melhor Ret%",
                border_style="blue",
                padding=(0, 0),
            ),
            name="arms",
            ratio=3,
        ),
        Layout(
            Panel(
                makers_t,
                title="[bold]Makers[/] ★=promissor (recal) · ·=viable PnL=?",
                border_style="green",
                padding=(0, 0),
            ),
            name="makers",
            ratio=2,
        ),
    )
    return layout


def run_live() -> None:
    import select
    import sys

    console = Console()
    cpu_cache: Dict[str, Any] = {}
    ok, _, pid = _alive(PID, HB)
    if ok:
        _cpu_pct(pid, cpu_cache)
        time.sleep(0.15)
        _cpu_pct(pid, cpu_cache)
    fd = sys.stdin.fileno()
    with Live(build_view(cpu_cache), console=console, auto_refresh=False, screen=True) as live:
        while True:
            # wait for a key OR the refresh interval (no blind sleep)
            deadline = time.time() + REFRESH_S
            quit_now = False
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                r, _, _ = select.select([fd], [], [], left)
                if r:
                    data = os.read(fd, 64)  # raw bytes, unbuffered
                    if not data or any(b in data for b in (b"q", b"Q", b"\x1b", b"\x03")):
                        quit_now = True
                        break
            if quit_now:
                break
            live.update(build_view(cpu_cache), refresh=True)


def capture_frame(path: Path, width: int = 140, height: int = 48) -> str:
    """Compact full dump (Group, no Layout stretch) for --once/--capture."""
    console = Console(record=True, width=width, force_terminal=True, color_system=None)
    cpu_cache: Dict[str, Any] = {}
    ok, _, pid = _alive(PID, HB)
    if ok:
        _cpu_pct(pid, cpu_cache)
        time.sleep(0.15)
        _cpu_pct(pid, cpu_cache)
    # reuse build_view panels via a throwaway layout render first...
    # Simpler: render live layout at modest height then fall back.
    layout = build_view(cpu_cache)
    console.print(layout, height=min(max(height, 42), 48))
    text = console.export_text(styles=False)
    if "A20_CMP" not in text:
        console2 = Console(record=True, width=width, force_terminal=True, color_system=None)
        console2.print(layout, height=52)
        text = console2.export_text(styles=False)
    path.write_text(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description="mm-lab painel (read-only)")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--capture", type=str, default="", help="write frame to file")
    ap.add_argument("--width", type=int, default=140)
    args = ap.parse_args()
    if args.once or args.capture:
        out = Path(args.capture) if args.capture else DATA / "painel_frame.txt"
        text = capture_frame(out, width=args.width)
        if args.once and not args.capture:
            print(text)
        else:
            print(f"captured {out} ({len(text)} chars)")
        return
    import sys
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        run_live()
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
