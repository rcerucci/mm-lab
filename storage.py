"""SQLite + rotating logs for mm-lab. Disk-aware."""
from __future__ import annotations

import gzip
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import DATA, KEEP_ROTATED, ROTATE_BYTES, SQLITE_PATH


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < ROTATE_BYTES:
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    rotated = path.with_name(f"{path.name}.{ts}")
    path.rename(rotated)
    gz = rotated.with_suffix(rotated.suffix + ".gz")
    with open(rotated, "rb") as src, gzip.open(gz, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    rotated.unlink(missing_ok=True)
    # prune
    pattern = f"{path.name}."
    old = sorted(path.parent.glob(f"{path.name}.*.gz"), reverse=True)
    for f in old[KEEP_ROTATED:]:
        f.unlink(missing_ok=True)


def append_jsonl(path: Path, line: str) -> None:
    _rotate(path)
    with open(path, "a") as f:
        f.write(line if line.endswith("\n") else line + "\n")


class Store:
    def __init__(self, path: Path = SQLITE_PATH) -> None:
        self.path = path
        # One connection shared across asyncio.to_thread workers — must serialize.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._init()

    def _init(self) -> None:
        with self._lock:
            c = self.conn
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_state (
                  t REAL NOT NULL,
                  venue TEXT NOT NULL,
                  coin TEXT NOT NULL,
                  mid REAL,
                  best_bid REAL,
                  best_ask REAL,
                  imbalance REAL,
                  rvol_1m REAL,
                  volume_1m REAL,
                  funding REAL,
                  spike INTEGER,
                  PRIMARY KEY (t, venue, coin)
                );
                CREATE TABLE IF NOT EXISTS maker_snap (
                  t REAL NOT NULL,
                  venue TEXT NOT NULL,
                  coin TEXT NOT NULL,
                  maker_id TEXT NOT NULL,
                  n_bid INTEGER,
                  n_ask INTEGER,
                  near_bps REAL,
                  far_bps REAL,
                  size_usd_near REAL,
                  size_usd_far REAL,
                  skew REAL,
                  paused INTEGER,
                  PRIMARY KEY (t, venue, coin, maker_id)
                );
                CREATE TABLE IF NOT EXISTS maker_stats (
                  venue TEXT NOT NULL,
                  maker_id TEXT NOT NULL,
                  coin TEXT NOT NULL,
                  first_seen REAL,
                  last_seen REAL,
                  n_snaps INTEGER,
                  median_cycle_s REAL,
                  median_near_bps REAL,
                  median_size_usd REAL,
                  pause_frac REAL,
                  est_pnl REAL,
                  maker_fills INTEGER,
                  viable INTEGER,
                  PRIMARY KEY (venue, maker_id, coin)
                );
                CREATE TABLE IF NOT EXISTS events (
                  t REAL NOT NULL,
                  kind TEXT NOT NULL,
                  payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_market_t ON market_state(t);
                CREATE INDEX IF NOT EXISTS idx_maker_snap_t ON maker_snap(t);
                CREATE INDEX IF NOT EXISTS idx_events_t ON events(t);
                CREATE TABLE IF NOT EXISTS maker_hist (
                  t REAL NOT NULL,
                  venue TEXT NOT NULL,
                  maker_id TEXT NOT NULL,
                  coin TEXT NOT NULL,
                  first_seen REAL,
                  last_seen REAL,
                  n_snaps INTEGER,
                  median_cycle_s REAL,
                  cycle_usable INTEGER,
                  median_near_bps REAL,
                  median_size_usd REAL,
                  pause_frac REAL,
                  est_pnl REAL,
                  maker_fills INTEGER,
                  viable INTEGER,
                  promissor INTEGER,
                  pnl_known INTEGER,
                  poll_gap_s REAL
                );
                CREATE INDEX IF NOT EXISTS idx_maker_hist_t ON maker_hist(t);
                CREATE INDEX IF NOT EXISTS idx_maker_hist_promissor ON maker_hist(promissor, t);
                """
            )
            c.commit()
            # soft migrations
            cols = {r[1] for r in c.execute("PRAGMA table_info(maker_stats)").fetchall()}
            if "promissor" not in cols:
                c.execute("ALTER TABLE maker_stats ADD COLUMN promissor INTEGER DEFAULT 0")
            if "pnl_known" not in cols:
                c.execute("ALTER TABLE maker_stats ADD COLUMN pnl_known INTEGER DEFAULT 0")
            c.commit()

    def insert_maker_hist(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO maker_hist(
                       t,venue,maker_id,coin,first_seen,last_seen,n_snaps,
                       median_cycle_s,cycle_usable,median_near_bps,median_size_usd,
                       pause_frac,est_pnl,maker_fills,viable,promissor,pnl_known,
                       poll_gap_s
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["t"],
                    row["venue"],
                    row["maker_id"],
                    row["coin"],
                    row.get("first_seen"),
                    row.get("last_seen"),
                    row.get("n_snaps", 0),
                    row.get("median_cycle_s"),
                    1 if row.get("cycle_usable") else 0,
                    row.get("median_near_bps"),
                    row.get("median_size_usd"),
                    row.get("pause_frac"),
                    row.get("est_pnl"),
                    row.get("maker_fills", 0),
                    1 if row.get("viable") else 0,
                    1 if row.get("promissor") else 0,
                    1 if row.get("pnl_known") else 0,
                    row.get("poll_gap_s"),
                ),
            )

    def promissor_hist(self, since: float) -> List[Dict[str, Any]]:
        """Compact 14d history of promissor snapshots (one vote later per maker)."""
        with self._lock:
            cur = self.conn.execute(
                """SELECT t,venue,maker_id,coin,first_seen,last_seen,n_snaps,
                          median_cycle_s,cycle_usable,median_near_bps,median_size_usd,
                          pause_frac,est_pnl,maker_fills,viable,promissor,pnl_known,
                          poll_gap_s
                   FROM maker_hist
                   WHERE promissor=1 AND t >= ?""",
                (since,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def insert_market(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO market_state
                   (t,venue,coin,mid,best_bid,best_ask,imbalance,rvol_1m,volume_1m,funding,spike)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["t"],
                    row["venue"],
                    row["coin"],
                    row.get("mid"),
                    row.get("best_bid"),
                    row.get("best_ask"),
                    row.get("imbalance"),
                    row.get("rvol_1m"),
                    row.get("volume_1m"),
                    row.get("funding"),
                    1 if row.get("spike") else 0,
                ),
            )

    def insert_maker_snap(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO maker_snap
                   (t,venue,coin,maker_id,n_bid,n_ask,near_bps,far_bps,size_usd_near,size_usd_far,skew,paused)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["t"],
                    row["venue"],
                    row["coin"],
                    row["maker_id"],
                    row.get("n_bid", 0),
                    row.get("n_ask", 0),
                    row.get("near_bps"),
                    row.get("far_bps"),
                    row.get("size_usd_near"),
                    row.get("size_usd_far"),
                    row.get("skew"),
                    1 if row.get("paused") else 0,
                ),
            )

    def upsert_maker_stats(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO maker_stats
                   (venue,maker_id,coin,first_seen,last_seen,n_snaps,median_cycle_s,
                    median_near_bps,median_size_usd,pause_frac,est_pnl,maker_fills,viable,
                    promissor,pnl_known)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(venue,maker_id,coin) DO UPDATE SET
                     last_seen=excluded.last_seen,
                     n_snaps=excluded.n_snaps,
                     median_cycle_s=excluded.median_cycle_s,
                     median_near_bps=excluded.median_near_bps,
                     median_size_usd=excluded.median_size_usd,
                     pause_frac=excluded.pause_frac,
                     est_pnl=CASE
                       WHEN excluded.pnl_known=1 THEN excluded.est_pnl
                       WHEN maker_stats.pnl_known=1 THEN maker_stats.est_pnl
                       ELSE excluded.est_pnl END,
                     maker_fills=MAX(maker_stats.maker_fills, excluded.maker_fills),
                     viable=excluded.viable,
                     promissor=excluded.promissor,
                     pnl_known=MAX(maker_stats.pnl_known, excluded.pnl_known)
                """,
                (
                    row["venue"],
                    row["maker_id"],
                    row["coin"],
                    row.get("first_seen"),
                    row.get("last_seen"),
                    row.get("n_snaps", 0),
                    row.get("median_cycle_s"),
                    row.get("median_near_bps"),
                    row.get("median_size_usd"),
                    row.get("pause_frac"),
                    row.get("est_pnl"),
                    row.get("maker_fills", 0),
                    1 if row.get("viable") else 0,
                    1 if row.get("promissor") else 0,
                    1 if row.get("pnl_known") else 0,
                ),
            )

    def scrub_stale_viable(
        self,
        min_near: float,
        max_near: float,
        min_cycle: float,
        min_size: float,
        max_pause: float,
    ) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE maker_stats SET viable=0, promissor=0
                   WHERE viable=1 AND (
                     median_near_bps IS NULL
                     OR median_near_bps < ?
                     OR median_near_bps > ?
                     OR median_cycle_s IS NULL
                     OR median_cycle_s < ?
                     OR (median_size_usd IS NOT NULL AND median_size_usd < ?)
                     OR pause_frac > ?
                   )""",
                (min_near, max_near, min_cycle, min_size, max_pause),
            )
            # known losers never promissor; unknown stay only if already flagged
            self.conn.execute(
                """UPDATE maker_stats SET promissor=0
                   WHERE promissor=1 AND pnl_known=1 AND (est_pnl IS NULL OR est_pnl <= 0)"""
            )

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def prune(self, older_than: float) -> None:
        """Drop raw snaps older than window; keep maker_stats."""
        with self._lock:
            self.conn.execute("DELETE FROM market_state WHERE t < ?", (older_than,))
            self.conn.execute("DELETE FROM maker_snap WHERE t < ?", (older_than,))
            self.conn.execute("DELETE FROM events WHERE t < ?", (older_than,))
            self.conn.execute("DELETE FROM maker_hist WHERE t < ?", (older_than,))
            self.conn.commit()
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass

    def viable_makers(self, since: float) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(
                """SELECT venue,maker_id,coin,median_cycle_s,median_near_bps,
                          median_size_usd,pause_frac,est_pnl,maker_fills,n_snaps,
                          promissor,pnl_known
                   FROM maker_stats
                   WHERE viable=1 AND last_seen >= ?""",
                (since,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def promissor_makers(self, since: float) -> List[Dict[str, Any]]:
        """Makers eligible for B/C/D recalibration medians."""
        with self._lock:
            cur = self.conn.execute(
                """SELECT venue,maker_id,coin,median_cycle_s,median_near_bps,
                          median_size_usd,pause_frac,est_pnl,maker_fills,n_snaps,
                          promissor,pnl_known
                   FROM maker_stats
                   WHERE promissor=1 AND viable=1 AND last_seen >= ?""",
                (since,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def recent_maker_seed(self, since: float):
        with self._lock:
            cur = self.conn.execute(
                """SELECT venue, maker_id, coin, est_pnl, maker_fills,
                          COALESCE(pnl_known,0), n_snaps, median_cycle_s
                   FROM maker_stats WHERE last_seen > ?""",
                (since,),
            )
            return cur.fetchall()

    def insert_event(self, t: float, kind: str, payload: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(t,kind,payload) VALUES (?,?,?)",
                (t, kind, payload),
            )

    def recent_market_regimes(self, since: float) -> List[Tuple[float, float, float]]:
        """Return (t, rvol_1m, imbalance) for HL BTC when available."""
        with self._lock:
            cur = self.conn.execute(
                """SELECT t, rvol_1m, imbalance FROM market_state
                   WHERE t >= ? AND venue='hl' AND coin='BTC'
                   ORDER BY t""",
                (since,),
            )
            return [(r[0], r[1] or 0.0, r[2] or 0.0) for r in cur.fetchall()]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            out = {}
            for table in ("market_state", "maker_snap", "maker_stats", "maker_hist", "events"):
                out[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            return out

    def close(self) -> None:
        with self._lock:
            self.conn.close()
