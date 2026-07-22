# FTMO Paper Trading Bot

Production-grade algorithmic paper trading bot for EUR/USD with trend-following pullback strategy, ADX confirmation, FTMO-compliant risk management, and a local Flask monitoring dashboard.

## Prerequisites

- macOS with Python 3.10+ installed
- A free [Tiingo](https://www.tiingo.com/) API token

Verify Python:

```bash
python3 --version
```

## Local Setup (macOS Terminal)

### 1. Navigate to the project

```bash
cd /path/to/FTMO-Bot
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and set your secrets:

```
TIINGO_SECRET=your_actual_tiingo_token
TRIGGER_SECRET=your_secure_random_key
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
```

### 5. Run the bot and dashboard (single process)

```bash
python dashboard.py
```

This starts:

- Flask dashboard at `http://127.0.0.1:5000`
- APScheduler job that runs `bot.execute()` every 15 minutes

### 6. Manual trigger (optional)

Trigger a bot execution immediately via the secure endpoint:

```bash
curl -X POST http://127.0.0.1:5000/trigger-bot-logic \
  -H "X-Trigger-Key: your_secure_random_key"
```

## Architecture

| Module | Purpose |
|---|---|
| `config.py` | Constants, risk parameters, indicator thresholds |
| `database.py` | SQLite WAL schema, thread-safe helpers |
| `bot.py` | Tiingo fetch, gap repair, strategy, risk engine |
| `dashboard.py` | Flask UI, APScheduler, secure trigger endpoint |

## Strategy Summary

- **Asset:** EUR/USD, 15-minute candles
- **Indicators:** 200 EMA, 14 RSI (Wilder), 50 ADX
- **LONG:** Close > EMA200, RSI < 35, ADX > 25
- **SHORT:** Close < EMA200, RSI > 65, ADX > 25
- **Risk:** 1% per trade (0.5% after 3 consecutive losses), 20 pip SL, 40 pip TP
- **Guards:** ±2% daily equity halt, weekend close, max 1 open trade

## Cloud Worker Deployment

For a dedicated cloud worker:

1. Clone the repo and follow setup steps 2–4 above
2. Run under a process manager (systemd, supervisor) with restart policy
3. Bind to `0.0.0.0` only behind a firewall
4. Keep `.env` out of version control

Example systemd unit:

```ini
[Unit]
Description=FTMO Paper Trading Bot
After=network.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/FTMO-Bot
Environment=PATH=/opt/FTMO-Bot/.venv/bin
ExecStart=/opt/FTMO-Bot/.venv/bin/python dashboard.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Database

On first run, `trading_bot.db` is created with:

- WAL journal mode for concurrent reads (dashboard) and writes (bot)
- Initial balance of $100,000.00
- Tables: `trades`, `daily_balances`, `logs`, `candles`, `bot_state`
