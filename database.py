"""SQLite schema, WAL configuration, and thread-safe database helpers."""

from __future__ import annotations

import sqlite3
import threading
import traceback
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import config

_write_lock = threading.Lock()


def get_connection() -> sqlite3.Connection:
    """Create a SQLite connection with WAL mode enabled."""
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=config.SQLITE_TIMEOUT,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db() -> None:
    """Create tables, indexes, and seed initial data on first run."""
    with _write_lock:
        conn = get_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('LONG', 'SHORT')),
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    sl REAL NOT NULL,
                    tp REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('OPEN', 'CLOSED')),
                    open_time_utc TEXT NOT NULL,
                    close_time_utc TEXT,
                    pips_realized REAL,
                    commission REAL DEFAULT 0.0,
                    net_profit REAL,
                    lots REAL NOT NULL DEFAULT 0.01,
                    signal_candle_utc TEXT,
                    last_checked_candle_utc TEXT
                );

                CREATE TABLE IF NOT EXISTS daily_balances (
                    date_utc TEXT PRIMARY KEY,
                    starting_balance REAL NOT NULL,
                    current_equity REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    log_level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    exception_class TEXT,
                    stack_trace TEXT
                );

                CREATE TABLE IF NOT EXISTS candles (
                    candle_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    open_time_utc TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    UNIQUE(asset, open_time_utc)
                );

                CREATE INDEX IF NOT EXISTS idx_candles_open_time_utc
                    ON candles(open_time_utc);

                CREATE TABLE IF NOT EXISTS bot_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    operational_state TEXT NOT NULL DEFAULT 'LONG_LOOKOUT',
                    last_evaluated_candle_utc TEXT,
                    consecutive_losses INTEGER NOT NULL DEFAULT 0,
                    active_risk_pct REAL NOT NULL DEFAULT 0.01,
                    halted_until_utc TEXT,
                    last_api_call_monotonic REAL DEFAULT 0.0,
                    last_execution_utc TEXT
                );
                """
            )

            row = conn.execute("SELECT COUNT(*) AS cnt FROM daily_balances").fetchone()
            if row["cnt"] == 0:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                conn.execute(
                    """
                    INSERT INTO daily_balances (date_utc, starting_balance, current_equity)
                    VALUES (?, ?, ?)
                    """,
                    (today, config.INITIAL_BALANCE, config.INITIAL_BALANCE),
                )

            state_row = conn.execute("SELECT COUNT(*) AS cnt FROM bot_state").fetchone()
            if state_row["cnt"] == 0:
                conn.execute(
                    """
                    INSERT INTO bot_state (
                        id, operational_state, consecutive_losses, active_risk_pct
                    ) VALUES (1, ?, 0, ?)
                    """,
                    (config.STATE_LONG_LOOKOUT, config.BASE_RISK_PCT),
                )

            conn.commit()
        finally:
            conn.close()


def log_event(
    level: str,
    message: str,
    exc: BaseException | None = None,
) -> None:
    """Write a structured log entry to the logs table."""
    exception_class = None
    stack_trace = None
    if exc is not None:
        exception_class = type(exc).__name__
        stack_trace = traceback.format_exc()

    with _write_lock:
        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO logs (timestamp_utc, log_level, message, exception_class, stack_trace)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_utc_now_iso(), level, message, exception_class, stack_trace),
            )
            conn.commit()
        finally:
            conn.close()


def get_bot_state() -> dict[str, Any]:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM bot_state WHERE id = 1").fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def update_bot_state(**fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    with _write_lock:
        conn = get_connection()
        try:
            conn.execute(f"UPDATE bot_state SET {columns} WHERE id = 1", values)
            conn.commit()
        finally:
            conn.close()


def get_or_create_daily_balance(date_utc: str) -> dict[str, Any]:
    """Return today's balance row, rolling equity forward on new UTC day."""
    with _write_lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM daily_balances WHERE date_utc = ?",
                (date_utc,),
            ).fetchone()

            if row:
                return dict(row)

            prev = conn.execute(
                """
                SELECT current_equity FROM daily_balances
                ORDER BY date_utc DESC LIMIT 1
                """
            ).fetchone()
            equity = prev["current_equity"] if prev else config.INITIAL_BALANCE

            conn.execute(
                """
                INSERT INTO daily_balances (date_utc, starting_balance, current_equity)
                VALUES (?, ?, ?)
                """,
                (date_utc, equity, equity),
            )
            conn.commit()
            return {
                "date_utc": date_utc,
                "starting_balance": equity,
                "current_equity": equity,
            }
        finally:
            conn.close()


def update_daily_equity(date_utc: str, equity: float) -> None:
    with _write_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE daily_balances SET current_equity = ? WHERE date_utc = ?",
                (equity, date_utc),
            )
            conn.commit()
        finally:
            conn.close()


def upsert_candles(candles: list[dict[str, Any]]) -> int:
    """Insert candles, ignoring duplicates. Returns count of new rows."""
    if not candles:
        return 0

    with _write_lock:
        conn = get_connection()
        try:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO candles
                    (asset, open_time_utc, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        c["asset"],
                        c["open_time_utc"],
                        c["open"],
                        c["high"],
                        c["low"],
                        c["close"],
                    )
                    for c in candles
                ],
            )
            conn.commit()
            return conn.total_changes - before
        finally:
            conn.close()


def get_candles_df(asset: str = config.ASSET) -> pd.DataFrame:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT open_time_utc, open, high, low, close
            FROM candles WHERE asset = ?
            ORDER BY open_time_utc ASC
            """,
            (asset,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return pd.DataFrame(columns=["open_time_utc", "open", "high", "low", "close"])

    df = pd.DataFrame([dict(r) for r in rows])
    df["open_time_utc"] = pd.to_datetime(df["open_time_utc"], utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def get_latest_candle_time(asset: str = config.ASSET) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT open_time_utc FROM candles
            WHERE asset = ? ORDER BY open_time_utc DESC LIMIT 1
            """,
            (asset,),
        ).fetchone()
        return row["open_time_utc"] if row else None
    finally:
        conn.close()


def detect_gaps(asset: str = config.ASSET) -> list[tuple[str, str]]:
    """Return list of (gap_start, gap_end) ISO timestamps for missing 15m bars."""
    df = get_candles_df(asset)
    if len(df) < 2:
        return []

    gaps: list[tuple[str, str]] = []
    interval = pd.Timedelta(minutes=config.CANDLE_INTERVAL_MIN)

    for i in range(1, len(df)):
        prev_time = df.iloc[i - 1]["open_time_utc"]
        curr_time = df.iloc[i]["open_time_utc"]
        expected = prev_time + interval
        if curr_time > expected:
            gaps.append(
                (
                    expected.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    (curr_time - interval).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )
    return gaps


def get_open_trade() -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY trade_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def open_trade(
    asset: str,
    direction: str,
    entry_price: float,
    sl: float,
    tp: float,
    open_time_utc: str,
    lots: float,
    signal_candle_utc: str,
) -> int:
    with _write_lock:
        conn = get_connection()
        try:
            cursor = conn.execute(
                """
                INSERT INTO trades (
                    asset, direction, entry_price, sl, tp, status,
                    open_time_utc, lots, signal_candle_utc, last_checked_candle_utc
                ) VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
                """,
                (
                    asset,
                    direction,
                    entry_price,
                    sl,
                    tp,
                    open_time_utc,
                    lots,
                    signal_candle_utc,
                    signal_candle_utc,
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def close_trade(
    trade_id: int,
    exit_price: float,
    close_time_utc: str,
    pips_realized: float,
    commission: float,
    net_profit: float,
) -> None:
    with _write_lock:
        conn = get_connection()
        try:
            conn.execute(
                """
                UPDATE trades SET
                    exit_price = ?, close_time_utc = ?, pips_realized = ?,
                    commission = ?, net_profit = ?, status = 'CLOSED'
                WHERE trade_id = ?
                """,
                (exit_price, close_time_utc, pips_realized, commission, net_profit, trade_id),
            )
            conn.commit()
        finally:
            conn.close()


def update_trade_last_checked(trade_id: int, candle_utc: str) -> None:
    with _write_lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE trades SET last_checked_candle_utc = ? WHERE trade_id = ?",
                (candle_utc, trade_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_recent_trades(limit: int = 10) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM trades ORDER BY trade_id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_closed_trades_stats() -> dict[str, float]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT net_profit FROM trades
            WHERE status = 'CLOSED' AND net_profit IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"win_rate": 0.0, "profit_factor": 0.0, "total_trades": 0}

    profits = [r["net_profit"] for r in rows]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    total = len(profits)
    win_rate = (len(wins) / total * 100) if total else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    if gross_losses == 0:
        profit_factor = gross_wins if gross_wins > 0 else 0.0
    else:
        profit_factor = gross_wins / gross_losses

    return {
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "total_trades": total,
    }


def get_current_equity() -> float:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT current_equity FROM daily_balances ORDER BY date_utc DESC LIMIT 1"
        ).fetchone()
        return row["current_equity"] if row else config.INITIAL_BALANCE
    finally:
        conn.close()
