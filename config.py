"""Configuration constants, risk parameters, and indicator thresholds."""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# --- Market ---
ASSET = "EURUSD"
PIP = 0.0001
PRICE_DECIMALS = 5
CANDLE_INTERVAL_MIN = 15

# --- Indicators ---
EMA_PERIOD = 200
RSI_PERIOD = 14
ADX_PERIOD = 50
WARMUP_CANDLES = 60

# --- Strategy thresholds ---
RSI_LONG_MAX = 35
RSI_SHORT_MIN = 65
ADX_MIN = 25

# --- Risk ---
INITIAL_BALANCE = 100_000.0
BASE_RISK_PCT = 0.01
REDUCED_RISK_PCT = 0.005
CONSECUTIVE_LOSS_LIMIT = 3
SL_PIPS = 20
TP_PIPS = 40
MIN_LOT_SIZE = 0.01

# --- Friction ---
ENTRY_PENALTY_PIPS = 2.0
COMMISSION_PER_LOT = 3.0
STANDARD_LOT_UNITS = 100_000
PIP_VALUE_PER_LOT = 10.0

# --- Guards ---
DAILY_PROFIT_HALT_PCT = 0.02
DAILY_LOSS_HALT_PCT = -0.02
FTMO_MAX_DAILY_LOSS_PCT = 0.05

# --- Weekend (DST-proof via America/New_York) ---
NY_TZ = ZoneInfo("America/New_York")
FRIDAY_CLOSE_UTC_STANDARD = 21  # 21:00 UTC (winter)
FRIDAY_CLOSE_UTC_DST = 20  # 20:00 UTC (summer)
SUNDAY_OPEN_HOUR_NY = 20  # 20:00 America/New_York

# --- Tiingo API ---
TIINGO_BASE_URL = "https://api.tiingo.com/tiingo/fx/eurusd/prices"
TIINGO_RESAMPLE_FREQ = "15min"
API_MIN_INTERVAL_SEC = 1.0
BOOTSTRAP_LOOKBACK_DAYS = 10

# --- Database ---
DB_PATH = "trading_bot.db"
SQLITE_TIMEOUT = 30.0

# --- Flask ---
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# --- Secrets ---
TIINGO_SECRET = os.getenv("TIINGO_SECRET", "")
TRIGGER_SECRET = os.getenv("TRIGGER_SECRET", "")

# --- Operational states ---
STATE_LONG_LOOKOUT = "LONG_LOOKOUT"
STATE_SHORT_LOOKOUT = "SHORT_LOOKOUT"
STATE_WEEKEND_MODE = "WEEKEND_MODE"
STATE_HALTED = "HALTED"
