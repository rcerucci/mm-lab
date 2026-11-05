#!/usr/bin/env python3
"""Compact status for mm-lab observer + $100 FIX/CMP simulator."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE = DATA / "sim_state.json"
HB = DATA / "heartbeat"
PID = DATA / "lab.pid"
DB = DATA / "observer.sqlite"


def alive() -> tuple[bool, str]:
    if not PID.exists() or not HB.exists():
        return False, "sem pid/heartbeat"
    try:
        pid = int(PID.read_text().strip())
        os.kill(pid, 0)
    except Exception as e:
        return False, f"pid morto ({e})"
    age = time.time() - float(HB.read_text().strip())
    if age > 90:
        return False, f"heartbeat velho ({age:.0f}s)"
    return True, f"pid={pid} heartbeat={age:.0f}s atrás"


def main() -> None:
    print(
        f"Agora: {datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y-%m-%d %H:%M:%S America/Sao_Paulo')}"
    )
    ok, msg = alive()
    print(f"Processo: {'RODANDO' if ok else 'PARADO'} ({msg})")
    if STATE.exists():
        raw = json.loads(STATE.read_text())
        started = raw.get("started_at", 0)
        saved = raw.get("saved_at", 0)
        uptime_h = (time.time() - started) / 3600 if started else 0
        print(
            f"Uptime: {uptime_h:.2f} h | salvo há {time.time()-saved:.0f}s | "
            f"schema={raw.get('schema')} initial=${raw.get('initial_equity')}"
        )
        print(f"Trades: {raw.get('n_trades')} | livros: {raw.get('n_books')}")
        mids = raw.get("mids") or {}
        print("Mids:", ", ".join(f"{k}={v:.4f}" for k, v in mids.items()) or "(aguardando)")
        imb = raw.get("imbalance") or {}
        if imb:
            print("Imb:", ", ".join(f"{k}={v:+.3f}" for k, v in imb.items()))
        print()
        books = raw.get("books") or {}

        def sort_key(k):
            b = books[k]
            return (
                1 if b.get("neighbor") else 0,
                b.get("recal_key") or "",
                b.get("spread") or 0,
                0 if b.get("mode") == "FIX" else 1,
            )

        for key in sorted(books, key=sort_key):
            b = books[key]
            as1 = (b["as_1m_sum"] / b["as_1m_ntl"]) if b.get("as_1m_ntl") else None
            print(
                f"=== {key} mode={b.get('mode')} spread={b['spread']*1e4:.0f}bps "
                f"refresh={b['refresh_s']:.0f}s ==="
            )
            print(
                f"  equity=${b.get('equity',0):.2f} ret={b.get('ret_pct',0):+.2f}% "
                f"cap=${b.get('cap',0):.2f} inv/cap={b.get('inv_cap_pct',0):.1f}% "
                f"ord=${b.get('order_usd',0):.1f}"
            )
            print(
                f"  fills={b['n_fills']} (opt={b.get('n_fills_opt',0)}) "
                f"vol=${b['volume_usd']:.2f} pnl=${b.get('pnl_net',0):.4f} "
                f"fee2x=${b.get('pnl_net_2x',0):.4f} fund=${b.get('funding_pnl',0):.4f}"
            )
            if as1 is not None:
                def _a(tag):
                    n = b.get(f"as_{tag}_ntl")
                    return (b[f"as_{tag}_sum"] / n * 1e4) if n else float("nan")
                print(
                    "  AS bps: " + " | ".join(
                        f"{t} {_a(t):.2f}" for t in ("1s", "5s", "30s", "1m", "5m")
                    )
                )
            nxt = b.get("next_recal_s")
            if nxt is not None:
                print(f"  próxima recal: {nxt/3600:.2f} h")
            print()
    else:
        print("Sem sim_state.json ainda.")

    print("--- Observer ---")
    if DB.exists():
        con = sqlite3.connect(str(DB))
        for table in ("market_state", "maker_snap", "maker_stats"):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {n}")
        rows = con.execute(
            "SELECT venue, COUNT(*), SUM(viable) FROM maker_stats GROUP BY venue"
        ).fetchall()
        for venue, n, v in rows:
            print(f"  makers {venue}: {n} (viable={v or 0})")
        con.close()
    else:
        print("  sem observer.sqlite ainda")


if __name__ == "__main__":
    main()
