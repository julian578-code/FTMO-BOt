"""Core execution loop, Tiingo API fetching, gap repair, and strategy calculations."""

from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

import config
import database as db


# ---------------------------------------------------------------------------
# UTC candle clock utilities
# ---------------------------------------------------------------------------

def floor_to_15m(dt: datetime) -> datetime:
    """Align a UTC datetime to the 15-minute candle open."""
    minute = (dt.minute // config.CANDLE_INTERVAL_MIN) * config.CANDLE_INTERVAL_MIN
    return dt.replace(minute=minute, second=0, microsecond=0)


def latest_closed_candle_open(now_utc: datetime | None = None) -> datetime:
    """Return open time of the latest fully closed 15m candle."""
    now = now_utc or datetime.now(timezone.utc)
    current_open = floor_to_15m(now)
    return current_open - timedelta(minutes=config.CANDLE_INTERVAL_MIN)


def to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_price(price: float) -> float:
    return round(price, config.PRICE_DECIMALS)


def pips_to_price(pips: float) -> float:
    return round(pips * config.PIP, config.PRICE_DECIMALS)


# ---------------------------------------------------------------------------
# Weekend / halt guards
# ---------------------------------------------------------------------------

def _is_us_dst(dt_utc: datetime) -> bool:
    """Detect US Eastern DST for the given UTC moment."""
    ny = dt_utc.astimezone(config.NY_TZ)
    return bool(ny.dst())


def is_weekend_mode(now_utc: datetime | None = None) -> bool:
    """True from Friday's configured cutoff until Sunday FX open."""
    now = now_utc or datetime.now(timezone.utc)
    weekday = now.weekday()  # Monday=0, Friday=4, Saturday=5, Sunday=6

    if weekday == 4:  # Friday
        close_hour = (
            config.FRIDAY_CLOSE_UTC_DST
            if _is_us_dst(now)
            else config.FRIDAY_CLOSE_UTC_STANDARD
        )
        cutoff = now.replace(hour=close_hour, minute=0, second=0, microsecond=0)
        return now >= cutoff

    if weekday == 5:  # Saturday
        return True

    if weekday == 6:  # Sunday
        ny = now.astimezone(config.NY_TZ)
        market_open = ny.replace(hour=config.SUNDAY_OPEN_HOUR_NY, minute=0, second=0, microsecond=0)
        return now < market_open.astimezone(timezone.utc)

    return False


def next_midnight_utc(now_utc: datetime | None = None) -> datetime:
    now = now_utc or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow


# ---------------------------------------------------------------------------
# Tiingo API client
# ---------------------------------------------------------------------------

def _enforce_rate_limit() -> None:
    state = db.get_bot_state()
    last_call = state.get("last_api_call_monotonic") or 0.0
    elapsed = time.monotonic() - last_call
    if elapsed < config.API_MIN_INTERVAL_SEC:
        time.sleep(config.API_MIN_INTERVAL_SEC - elapsed)
    db.update_bot_state(last_api_call_monotonic=time.monotonic())


def fetch_tiingo_candles(start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    """Fetch 15m EUR/USD candles from Tiingo REST API."""
    if not config.TIINGO_SECRET:
        raise ValueError("TIINGO_SECRET is not configured")

    _enforce_rate_limit()

    params: dict[str, str] = {
        "startDate": start_date,
        "resampleFreq": config.TIINGO_RESAMPLE_FREQ,
    }
    if end_date:
        params["endDate"] = end_date

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Token {config.TIINGO_SECRET}",
    }

    response = requests.get(config.TIINGO_BASE_URL, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    db.update_bot_state(last_api_call_monotonic=time.monotonic())

    raw = response.json()
    candles: list[dict[str, Any]] = []
    for row in raw:
        dt = pd.to_datetime(row["date"], utc=True)
        open_time = floor_to_15m(dt.to_pydatetime())
        candles.append(
            {
                "asset": config.ASSET,
                "open_time_utc": to_iso_utc(open_time),
                "open": round_price(float(row["open"])),
                "high": round_price(float(row["high"])),
                "low": round_price(float(row["low"])),
                "close": round_price(float(row["close"])),
            }
        )
    return candles


def sync_candles() -> None:
    """Bootstrap or update candle cache, repairing gaps."""
    df = db.get_candles_df()
    now = datetime.now(timezone.utc)

    if len(df) < config.WARMUP_CANDLES:
        start = (now - timedelta(days=config.BOOTSTRAP_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        db.log_event("INFO", f"Bootstrapping candles from {start}")
        try:
            candles = fetch_tiingo_candles(start)
            inserted = db.upsert_candles(candles)
            db.log_event("INFO", f"Bootstrap inserted {inserted} candles")
        except Exception as exc:
            db.log_event("ERROR", "Failed to bootstrap candles from Tiingo", exc)
            return
    else:
        latest = db.get_latest_candle_time()
        if latest:
            latest_dt = pd.to_datetime(latest, utc=True).to_pydatetime()
            start = latest_dt.strftime("%Y-%m-%d")
        else:
            start = (now - timedelta(days=config.BOOTSTRAP_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

        try:
            candles = fetch_tiingo_candles(start)
            inserted = db.upsert_candles(candles)
            if inserted:
                db.log_event("INFO", f"Updated {inserted} new candles")
        except Exception as exc:
            db.log_event("ERROR", "Failed to fetch latest candles from Tiingo", exc)

    gaps = db.detect_gaps()
    for gap_start, gap_end in gaps:
        start_dt = pd.to_datetime(gap_start, utc=True)
        fetch_start = start_dt.strftime("%Y-%m-%d")
        db.log_event("INFO", f"Repairing gap {gap_start} -> {gap_end}")
        try:
            repaired = fetch_tiingo_candles(fetch_start)
            db.upsert_candles(repaired)
        except Exception as exc:
            db.log_event("ERROR", f"Gap repair failed for {gap_start}", exc)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi_wilder(close: pd.Series, period: int = config.RSI_PERIOD) -> pd.Series:
    """RSI using strict Wilder's exponential smoothing."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = pd.Series(index=close.index, dtype=float)
    avg_loss = pd.Series(index=close.index, dtype=float)

    if len(close) <= period:
        return pd.Series(index=close.index, dtype=float)

    avg_gain.iloc[period] = gain.iloc[1 : period + 1].mean()
    avg_loss.iloc[period] = loss.iloc[1 : period + 1].mean()

    for i in range(period + 1, len(close)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rsi = pd.Series(index=close.index, dtype=float)
    for i in range(period, len(close)):
        ag = avg_gain.iloc[i]
        al = avg_loss.iloc[i]
        if al == 0:
            rsi.iloc[i] = 100.0 if ag > 0 else 50.0
        else:
            rs = ag / al
            rsi.iloc[i] = 100 - (100 / (1 + rs))
    return rsi


def compute_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = config.ADX_PERIOD
) -> pd.Series:
    """Average Directional Index with Wilder smoothing."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    def wilder_smooth(series: pd.Series) -> pd.Series:
        result = pd.Series(index=series.index, dtype=float)
        if len(series) <= period:
            return result
        result.iloc[period] = series.iloc[1 : period + 1].sum()
        for i in range(period + 1, len(series)):
            result.iloc[i] = result.iloc[i - 1] - (result.iloc[i - 1] / period) + series.iloc[i]
        return result

    atr = wilder_smooth(tr)
    plus_di = 100 * wilder_smooth(plus_dm) / atr.replace(0, float("nan"))
    minus_di = 100 * wilder_smooth(minus_dm) / atr.replace(0, float("nan"))

    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan")) * 100
    adx = pd.Series(index=close.index, dtype=float)
    if len(close) > period * 2:
        adx.iloc[period * 2] = dx.iloc[period + 1 : period * 2 + 1].mean()
        for i in range(period * 2 + 1, len(close)):
            adx.iloc[i] = (adx.iloc[i - 1] * (period - 1) + dx.iloc[i]) / period
    return adx


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add EMA, RSI, ADX columns to candle dataframe."""
    result = df.copy()
    result["ema200"] = compute_ema(result["close"], config.EMA_PERIOD)
    result["rsi"] = compute_rsi_wilder(result["close"], config.RSI_PERIOD)
    result["adx"] = compute_adx(result["high"], result["low"], result["close"], config.ADX_PERIOD)
    return result


# ---------------------------------------------------------------------------
# Position sizing & friction
# ---------------------------------------------------------------------------

def calculate_lot_size(equity: float, risk_pct: float) -> float:
    risk_amount = equity * risk_pct
    lots = risk_amount / (config.SL_PIPS * config.PIP_VALUE_PER_LOT)
    return max(config.MIN_LOT_SIZE, round(lots, 2))


def apply_entry_friction(direction: str, base_price: float) -> float:
    penalty = pips_to_price(config.ENTRY_PENALTY_PIPS)
    if direction == "LONG":
        return round_price(base_price + penalty)
    return round_price(base_price - penalty)


def compute_sl_tp(direction: str, entry_price: float) -> tuple[float, float]:
    sl_dist = pips_to_price(config.SL_PIPS)
    tp_dist = pips_to_price(config.TP_PIPS)
    if direction == "LONG":
        return round_price(entry_price - sl_dist), round_price(entry_price + tp_dist)
    return round_price(entry_price + sl_dist), round_price(entry_price - tp_dist)


# ---------------------------------------------------------------------------
# Trade monitoring & closure
# ---------------------------------------------------------------------------

def _check_sl_tp_hit(
    direction: str, high: float, low: float, sl: float, tp: float
) -> tuple[str | None, bool]:
    """Return (exit_reason, dual_breach). exit_reason is 'SL', 'TP', or None."""
    if direction == "LONG":
        sl_hit = low <= sl
        tp_hit = high >= tp
    else:
        sl_hit = high >= sl
        tp_hit = low <= tp

    if sl_hit and tp_hit:
        return "SL", True
    if sl_hit:
        return "SL", False
    if tp_hit:
        return "TP", False
    return None, False


def _close_position(
    trade: dict[str, Any],
    exit_price: float,
    close_time_utc: str,
    reason: str,
    dual_breach: bool = False,
) -> float:
    """Close trade, update equity and consecutive loss state. Returns new equity."""
    direction = trade["direction"]
    entry = trade["entry_price"]
    lots = trade["lots"]

    if direction == "LONG":
        pips = (exit_price - entry) / config.PIP
    else:
        pips = (entry - exit_price) / config.PIP

    gross_pnl = pips * config.PIP_VALUE_PER_LOT * lots
    commission = config.COMMISSION_PER_LOT * lots
    net_profit = gross_pnl - commission

    db.close_trade(
        trade_id=trade["trade_id"],
        exit_price=exit_price,
        close_time_utc=close_time_utc,
        pips_realized=round(pips, 1),
        commission=commission,
        net_profit=round(net_profit, 2),
    )

    if dual_breach:
        db.log_event(
            "WARNING",
            f"Dual SL/TP breach on trade {trade['trade_id']}; assumed SL first (worst-case fill)",
        )
    else:
        db.log_event("INFO", f"Closed trade {trade['trade_id']} via {reason} at {exit_price}")

    equity = db.get_current_equity() + net_profit
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.update_daily_equity(today, equity)

    state = db.get_bot_state()
    consecutive = state.get("consecutive_losses", 0)
    if net_profit <= 0:
        consecutive += 1
        risk_pct = (
            config.REDUCED_RISK_PCT
            if consecutive >= config.CONSECUTIVE_LOSS_LIMIT
            else config.BASE_RISK_PCT
        )
        db.update_bot_state(consecutive_losses=consecutive, active_risk_pct=risk_pct)
    else:
        db.update_bot_state(
            consecutive_losses=0, active_risk_pct=config.BASE_RISK_PCT
        )

    return equity


def monitor_open_trade(df: pd.DataFrame, trade: dict[str, Any]) -> None:
    """Check SL/TP on candles after entry."""
    last_checked = trade.get("last_checked_candle_utc")
    if last_checked:
        mask = df["open_time_utc"] > pd.to_datetime(last_checked, utc=True)
    else:
        mask = df["open_time_utc"] >= pd.to_datetime(trade["open_time_utc"], utc=True)

    candles_to_check = df.loc[mask].sort_values("open_time_utc")

    for _, candle in candles_to_check.iterrows():
        reason, dual = _check_sl_tp_hit(
            trade["direction"],
            candle["high"],
            candle["low"],
            trade["sl"],
            trade["tp"],
        )
        candle_iso = to_iso_utc(candle["open_time_utc"].to_pydatetime())

        if reason:
            exit_price = trade["sl"] if reason == "SL" else trade["tp"]
            _close_position(trade, exit_price, candle_iso, reason, dual)
            return

        db.update_trade_last_checked(trade["trade_id"], candle_iso)


def force_close_trade(trade: dict[str, Any], close_time_utc: str, reason: str) -> None:
    """Force-close at latest close price (weekend/daily guard)."""
    df = db.get_candles_df()
    if df.empty:
        exit_price = trade["entry_price"]
    else:
        exit_price = round_price(float(df.iloc[-1]["close"]))
    _close_position(trade, exit_price, close_time_utc, reason)


# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------

def evaluate_signal(indicators: pd.DataFrame, signal_idx: int) -> str | None:
    """Evaluate LONG/SHORT signal on candle at signal_idx."""
    row = indicators.iloc[signal_idx]
    if pd.isna(row["ema200"]) or pd.isna(row["rsi"]) or pd.isna(row["adx"]):
        return None

    if (
        row["close"] > row["ema200"]
        and row["rsi"] < config.RSI_LONG_MAX
        and row["adx"] > config.ADX_MIN
    ):
        return "LONG"

    if (
        row["close"] < row["ema200"]
        and row["rsi"] > config.RSI_SHORT_MIN
        and row["adx"] > config.ADX_MIN
    ):
        return "SHORT"

    return None


def determine_operational_state(
    indicators: pd.DataFrame, halted: bool, weekend: bool
) -> str:
    if weekend:
        return config.STATE_WEEKEND_MODE
    if halted:
        return config.STATE_HALTED
    if indicators.empty:
        return config.STATE_LONG_LOOKOUT
    last = indicators.iloc[-1]
    if pd.isna(last["ema200"]):
        return config.STATE_LONG_LOOKOUT
    if last["close"] > last["ema200"]:
        return config.STATE_LONG_LOOKOUT
    return config.STATE_SHORT_LOOKOUT


# ---------------------------------------------------------------------------
# Risk guards
# ---------------------------------------------------------------------------

def apply_daily_guard(daily: dict[str, Any], now_utc: datetime) -> bool:
    """Check ±2% daily equity guard. Returns True if halted."""
    starting = daily["starting_balance"]
    equity = daily["current_equity"]
    pnl_pct = (equity - starting) / starting

    if pnl_pct >= config.DAILY_PROFIT_HALT_PCT or pnl_pct <= config.DAILY_LOSS_HALT_PCT:
        open_trade = db.get_open_trade()
        if open_trade:
            force_close_trade(open_trade, to_iso_utc(now_utc), "DAILY_GUARD")
        db.update_bot_state(
            operational_state=config.STATE_HALTED,
            halted_until_utc=to_iso_utc(next_midnight_utc(now_utc)),
        )
        db.log_event(
            "WARNING",
            f"Daily equity guard triggered at {pnl_pct:.2%}; halted until midnight UTC",
        )
        return True
    return False


def check_halt_expired(now_utc: datetime) -> bool:
    """Clear halt if we've passed midnight UTC."""
    state = db.get_bot_state()
    halted_until = state.get("halted_until_utc")
    if not halted_until:
        return False

    halt_dt = pd.to_datetime(halted_until, utc=True).to_pydatetime()
    if now_utc >= halt_dt:
        db.update_bot_state(halted_until_utc=None)
        return True
    return False


def apply_weekend_guard(now_utc: datetime) -> bool:
    """Force-close positions and set weekend mode. Returns True if weekend."""
    if not is_weekend_mode(now_utc):
        return False

    open_trade = db.get_open_trade()
    if open_trade:
        force_close_trade(open_trade, to_iso_utc(now_utc), "WEEKEND_GUARD")
        db.log_event("INFO", "Force-closed open trade for weekend")

    db.update_bot_state(operational_state=config.STATE_WEEKEND_MODE)
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_operational_state() -> str:
    state = db.get_bot_state()
    return state.get("operational_state", config.STATE_LONG_LOOKOUT)


def get_portfolio_metrics() -> dict[str, Any]:
    stats = db.get_closed_trades_stats()
    equity = db.get_current_equity()
    open_trade = db.get_open_trade()
    state = db.get_bot_state()

    return {
        "balance": equity,
        "win_rate": stats["win_rate"],
        "profit_factor": stats["profit_factor"],
        "total_trades": stats["total_trades"],
        "operational_state": state.get("operational_state", config.STATE_LONG_LOOKOUT),
        "open_trade": open_trade is not None,
        "last_execution_utc": state.get("last_execution_utc"),
        "active_risk_pct": state.get("active_risk_pct", config.BASE_RISK_PCT),
    }


def execute():
    """Main bot execution pipeline — called every 15 minutes."""
    now_utc = datetime.now(timezone.utc)
    today = now_utc.strftime("%Y-%m-%d")

    try:
        # Zorgt ervoor dat de tabellen altijd bestaan voordat we data ophalen
        db.init_db()  # <--- DEZE REGEL IS TOEGEVOEGD!

        daily = db.get_or_create_daily_balance(today)
        state = db.get_bot_state()

        check_halt_expired(now_utc)
        state = db.get_bot_state()

        halted_until = state.get("halted_until_utc")
        if halted_until and pd.to_datetime(halted_until, utc=True).to_pydatetime() > now_utc:
            db.update_bot_state(
                operational_state=config.STATE_HALTED,
                last_execution_utc=to_iso_utc(now_utc),
            )
            return

        if apply_weekend_guard(now_utc):
            db.update_bot_state(last_execution_utc=to_iso_utc(now_utc))
            return

        if apply_daily_guard(daily, now_utc):
            db.update_bot_state(last_execution_utc=to_iso_utc(now_utc))
            return

        sync_candles()

        df = db.get_candles_df()
        if len(df) < config.WARMUP_CANDLES:
            db.log_event(
                "INFO",
                f"Insufficient candles ({len(df)}/{config.WARMUP_CANDLES}); waiting for warm-up",
            )
            db.update_bot_state(last_execution_utc=to_iso_utc(now_utc))
            return

        closed_open = latest_closed_candle_open(now_utc)
        closed_iso = to_iso_utc(closed_open)

        last_evaluated = state.get("last_evaluated_candle_utc")
        if last_evaluated and closed_iso <= last_evaluated:
            db.update_bot_state(last_execution_utc=to_iso_utc(now_utc))
            return

        indicators = compute_indicators(df)

        closed_mask = indicators["open_time_utc"] <= closed_open
        closed_df = indicators.loc[closed_mask]
        if closed_df.empty:
            db.update_bot_state(last_execution_utc=to_iso_utc(now_utc))
            return

        signal_idx = closed_df.index[-1]
        signal_row = indicators.loc[signal_idx]
        signal_candle_iso = to_iso_utc(signal_row["open_time_utc"].to_pydatetime())

        open_trade = db.get_open_trade()
        if open_trade:
            monitor_open_trade(indicators, open_trade)
            open_trade = db.get_open_trade()

        is_halted = bool(
            state.get("halted_until_utc")
            and pd.to_datetime(state["halted_until_utc"], utc=True).to_pydatetime() > now_utc
        )

        if open_trade is None and not is_halted:
            signal = evaluate_signal(indicators, signal_idx)
            if signal:
                next_candle_time = signal_row["open_time_utc"] + pd.Timedelta(
                    minutes=config.CANDLE_INTERVAL_MIN
                )
                next_rows = indicators.loc[
                    indicators["open_time_utc"] == next_candle_time
                ]
                if not next_rows.empty:
                    entry_base = float(next_rows.iloc[0]["open"])
                    entry_price = apply_entry_friction(signal, entry_base)
                    sl, tp = compute_sl_tp(signal, entry_price)
                    equity = db.get_current_equity()
                    risk_pct = state.get("active_risk_pct", config.BASE_RISK_PCT)
                    lots = calculate_lot_size(equity, risk_pct)
                    entry_time_iso = to_iso_utc(next_candle_time.to_pydatetime())

                    trade_id = db.open_trade(
                        asset=config.ASSET,
                        direction=signal,
                        entry_price=entry_price,
                        sl=sl,
                        tp=tp,
                        open_time_utc=entry_time_iso,
                        lots=lots,
                        signal_candle_utc=signal_candle_iso,
                    )
                    db.log_event(
                        "INFO",
                        f"Opened {signal} trade #{trade_id} at {entry_price} "
                        f"({lots} lots, SL={sl}, TP={tp})",
                    )

        op_state = determine_operational_state(
            closed_df, is_halted, is_weekend_mode(now_utc)
        )
        db.update_bot_state(
            operational_state=op_state,
            last_evaluated_candle_utc=signal_candle_iso,
            last_execution_utc=to_iso_utc(now_utc),
        )

        equity = db.get_current_equity()
        db.update_daily_equity(today, equity)

    except Exception as exc:
        db.log_event("ERROR", f"execute() failed: {exc}", exc)
        db.update_bot_state(last_execution_utc=to_iso_utc(now_utc))