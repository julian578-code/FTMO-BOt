"""Flask monitoring dashboard with APScheduler and secure bot trigger."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, Response, request

import bot
import config
import database as db

app = Flask(__name__)
_execution_lock = threading.Lock()
_scheduler: BackgroundScheduler | None = None


def _run_bot_safe() -> None:
    """Execute bot logic with concurrency guard."""
    acquired = _execution_lock.acquire(blocking=False)
    if not acquired:
        db.log_event("INFO", "Skipped execution — another run is in progress")
        return
    try:
        bot.execute()
    finally:
        _execution_lock.release()


def _state_badge_class(state: str) -> str:
    mapping = {
        config.STATE_LONG_LOOKOUT: "badge-long",
        config.STATE_SHORT_LOOKOUT: "badge-short",
        config.STATE_WEEKEND_MODE: "badge-weekend",
        config.STATE_HALTED: "badge-halted",
    }
    return mapping.get(state, "badge-default")


def _render_dashboard() -> str:
    metrics = bot.get_portfolio_metrics()
    trades = db.get_recent_trades(10)
    state = metrics["operational_state"]
    badge_class = _state_badge_class(state)

    trade_rows = ""
    if trades:
        for t in trades:
            exit_p = f"{t['exit_price']:.5f}" if t["exit_price"] else "—"
            pips = f"{t['pips_realized']:.1f}" if t["pips_realized"] is not None else "—"
            net = f"${t['net_profit']:,.2f}" if t["net_profit"] is not None else "—"
            close_t = t["close_time_utc"] or "—"
            trade_rows += f"""
            <tr>
                <td>{t['trade_id']}</td>
                <td>{t['asset']}</td>
                <td class="dir-{t['direction'].lower()}">{t['direction']}</td>
                <td>{t['entry_price']:.5f}</td>
                <td>{exit_p}</td>
                <td>{pips}</td>
                <td>{net}</td>
                <td>{t['status']}</td>
                <td>{t['open_time_utc']}</td>
                <td>{close_t}</td>
            </tr>"""
    else:
        trade_rows = '<tr><td colspan="10" class="empty">No trades yet</td></tr>'

    open_indicator = "Yes" if metrics["open_trade"] else "No"
    last_exec = metrics["last_execution_utc"] or "Never"
    pf = metrics["profit_factor"]
    pf_display = f"{pf:.2f}" if pf != float("inf") else "∞"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="30">
    <title>FTMO Paper Trading Bot</title>
    <style>
        :root {{
            --bg: #0d1117;
            --surface: #161b22;
            --border: #30363d;
            --text: #e6edf3;
            --muted: #8b949e;
            --accent: #58a6ff;
            --green: #3fb950;
            --red: #f85149;
            --orange: #d29922;
            --purple: #bc8cff;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.5;
            padding: 2rem;
        }}
        .header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 2rem;
            flex-wrap: wrap;
            gap: 1rem;
        }}
        h1 {{ font-size: 1.5rem; font-weight: 600; }}
        .badge {{
            display: inline-block;
            padding: 0.35rem 0.85rem;
            border-radius: 2rem;
            font-size: 0.8rem;
            font-weight: 600;
            letter-spacing: 0.03em;
            text-transform: uppercase;
        }}
        .badge-long {{ background: #1a3a2a; color: var(--green); }}
        .badge-short {{ background: #3a1a1a; color: var(--red); }}
        .badge-weekend {{ background: #2a2a1a; color: var(--orange); }}
        .badge-halted {{ background: #3a1a2a; color: var(--purple); }}
        .badge-default {{ background: var(--surface); color: var(--muted); }}
        .metrics {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        .metric-card {{
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1.25rem;
        }}
        .metric-label {{
            font-size: 0.75rem;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.25rem;
        }}
        .metric-value {{
            font-size: 1.5rem;
            font-weight: 700;
        }}
        .metric-value.positive {{ color: var(--green); }}
        .section-title {{
            font-size: 1.1rem;
            margin-bottom: 1rem;
            color: var(--muted);
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
        }}
        th, td {{
            padding: 0.65rem 0.85rem;
            text-align: left;
            border-bottom: 1px solid var(--border);
            font-size: 0.85rem;
        }}
        th {{
            background: #1c2128;
            color: var(--muted);
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.7rem;
            letter-spacing: 0.05em;
        }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: #1c2128; }}
        .dir-long {{ color: var(--green); font-weight: 600; }}
        .dir-short {{ color: var(--red); font-weight: 600; }}
        .empty {{ text-align: center; color: var(--muted); padding: 2rem; }}
        .footer {{
            margin-top: 2rem;
            color: var(--muted);
            font-size: 0.8rem;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>FTMO Paper Trading Bot</h1>
        <span class="badge {badge_class}">{state.replace('_', ' ')}</span>
    </div>

    <div class="metrics">
        <div class="metric-card">
            <div class="metric-label">Simulated Balance</div>
            <div class="metric-value positive">${metrics['balance']:,.2f}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Win Rate</div>
            <div class="metric-value">{metrics['win_rate']:.1f}%</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Profit Factor</div>
            <div class="metric-value">{pf_display}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Open Trade</div>
            <div class="metric-value">{open_indicator}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Total Trades</div>
            <div class="metric-value">{metrics['total_trades']}</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Active Risk</div>
            <div class="metric-value">{metrics['active_risk_pct'] * 100:.1f}%</div>
        </div>
    </div>

    <h2 class="section-title">Recent Trades</h2>
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Asset</th>
                <th>Dir</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Pips</th>
                <th>Net P&amp;L</th>
                <th>Status</th>
                <th>Opened</th>
                <th>Closed</th>
            </tr>
        </thead>
        <tbody>
            {trade_rows}
        </tbody>
    </table>

    <div class="footer">
        <p>Last execution: {last_exec} UTC &middot; Auto-refresh every 30s &middot; Bot runs every 15 min</p>
    </div>
</body>
</html>"""


@app.route("/")
def index() -> str:
    return _render_dashboard()


@app.route("/trigger-bot-logic", methods=["POST"])
def trigger_bot_logic() -> tuple[Response, int]:
    trigger_key = request.headers.get("X-Trigger-Key", "")
    if not config.TRIGGER_SECRET or trigger_key != config.TRIGGER_SECRET:
        return Response("Unauthorized", status=401)

    thread = threading.Thread(target=_run_bot_safe, daemon=True)
    thread.start()
    return Response("Accepted", status=202)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _run_bot_safe,
        trigger=IntervalTrigger(minutes=15),
        id="bot_execute",
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    db.init_db()
    _scheduler = start_scheduler()
    db.log_event("INFO", "Dashboard started with 15-minute scheduler")
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False, use_reloader=False)
