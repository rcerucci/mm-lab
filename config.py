"""mm-lab constants. Read-only public APIs only. No keys/wallets/orders."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

# Hyperliquid
HL_WS = "wss://api.hyperliquid.xyz/ws"
HL_INFO = "https://api.hyperliquid.xyz/info"
HL_COINS = ["BTC", "HYPE"]
HL_MAKER_FEE = 0.00015
HL_MAKER_FEE_2X = 0.00030
HL_FUNDING_PERIOD_S = 3600.0
HL_MIN_ORDER_USD = 10.0  # Hyperliquid minimum notional
# Lot = 10**-szDecimals. Verified 05/11/2026 via public POST https://api.hyperliquid.xyz/info
# {"type":"meta"}: BTC szDecimals=5 (0.00001), HYPE szDecimals=2 (0.01).
HL_SZ_DECIMALS = {"BTC": 5, "HYPE": 2}

# Lighter
LIGHTER_REST = "https://mainnet.zklighter.elliot.ai"
LIGHTER_WS = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
LIGHTER_MARKETS = {"BTC": 1, "HYPE": 24}

# Latency model (sigma → HL)
LATENCY_API_S = 0.280
LATENCY_WS_S = 0.470

# Viable-for-us filter
VIABLE_MIN_CYCLE_S = 3.0
VIABLE_MIN_DIST_BPS = 10.0
VIABLE_MAX_DIST_BPS = 100.0
VIABLE_MIN_SIZE_USD = 50.0
VIABLE_MIN_LAYERS = 1.0
VIABLE_MAX_PAUSE_FRAC = 0.7

# Promissor = viable + profitable (or activity proxy when PnL unknown).
# Used for B/C/D recalibration medians. Known losers (est_pnl <= 0) excluded.
PROMISSOR_MIN_SNAPS = 80          # ~2 min at 1.5 s Lighter poll
PROMISSOR_MIN_PRESENCE_S = 300.0  # 5 min on book before activity proxy
PROMISSOR_MIN_FILLS_PROXY = 5     # kept for display; no longer an OR bypass
PROMISSOR_MIN_CYCLE_SAMPLES = 5   # real cycle observations (not seeded)
# Lighter has no reliable closedPnl; presence + stable cycle is the proxy.

# Simulator — faithful to a real US$100 1x account
INITIAL_EQUITY = 100.0
ORDER_USD_BASE = 15.0  # per side; floored by HL_MIN_ORDER_USD
BASE_SPREAD = 0.0010  # 10 bps
BASE_REFRESH_S = 30.0
SKEW_PER_USD = 2e-5  # stronger on $100 book (was 2e-6 on $10k)
SKEW_CAP_FRAC = 0.5
MID_JUMP_TRIGGER = 0.5
SPREAD_NEIGHBORS = [0.0005, 0.0020]  # 5 & 20 bps under A
BOOK_DEPTH_FOR_IMB = 5
BOOK_DEPTH_FOR_QUEUE = 10
CAP_MODES = ("FIX", "CMP")  # FIX=cap $100; CMP=cap tracks equity
INV_CAP_SOFT = 0.98  # quote only reducing side at/above this fraction of cap

# Recalibration (applied to both FIX and CMP copies)
ARMS = {
    "A": {"period_s": None, "label": "fixed"},
    "B": {"period_s": 86400, "label": "daily"},
    "C": {"period_s": 3 * 86400, "label": "every_3d"},
    "D": {"period_s": 7 * 86400, "label": "weekly"},
}
ROLLING_WINDOW_S = 14 * 86400
MAX_SPREAD_STEP = 0.0005
MAX_REFRESH_STEP_S = 10.0
MIN_SPREAD = 0.0005
RECAL_MIN_SPREAD = 0.0010  # 10 bps floor for B/C/D; A5 stays a fixed neighbor
MAX_SPREAD = 0.0050
MIN_REFRESH_S = 5.0
MAX_REFRESH_S = 120.0
HL_CYCLE_CENSOR_MULT = 2.0  # HL cycle must exceed 2x observe gap
AS_TOLERANCE_S = {"1s": 1.0, "5s": 2.0, "30s": 5.0, "1m": 15.0, "5m": 30.0}
MID_GAP_S = 3.0  # do not interpolate/LOCF across a hole larger than this
MAKER_HIST_STALE_S = 300.0  # skip maker_hist writes when last_seen is older
USERFILLS_WINDOW_S = 86400.0  # comparable maker PnL window
VERSION_PATH = ROOT / "VERSION"

# Observer cadence
LIGHTER_ORDERS_POLL_S = 1.5
HL_ADDR_HOT_POLL_S = 12.0
HL_ADDR_COLD_POLL_S = 60.0
HL_HOT_MAKERS = 8
MARKET_SNAP_S = 5.0
MAKER_SUMMARY_S = 30.0
MAX_TRACKED_LIGHTER = 80
MAX_TRACKED_HL = 40
SQLITE_PATH = DATA / "observer.sqlite"
SIM_STATE_PATH = DATA / "sim_state.json"
HEARTBEAT_PATH = DATA / "heartbeat"
PID_PATH = DATA / "lab.pid"
LOG_PATH = DATA / "lab.log"
FILLS_PATH = DATA / "fills.jsonl"
DASH_PATH = DATA / "dashboard.json"
LIVE_SOCK_PATH = DATA / "lab.sock"
PNL_HIST_PATH = DATA / "pnl_hist.jsonl"
ROTATE_BYTES = 40 * 1024 * 1024
KEEP_ROTATED = 3

# Back-compat aliases used by older imports
INITIAL_USDC = INITIAL_EQUITY
ORDER_USD = ORDER_USD_BASE
