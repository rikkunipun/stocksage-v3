# StockSage v3

A Telegram bot that sends a daily BUY/SELL briefing, with reasons, for an 80 to 100 stock NSE portfolio.

## The problem

I built this for my father, who manages an 80 to 100 stock portfolio on the NSE.
Every morning he checked it by hand, stock by stock, to decide what needed attention.
He needed a short list of what to act on and why, waiting for him before the market opens.

## What it does

- Sends a briefing to Telegram at 8:30 AM IST every trading day.
- Lists BUY and SELL signals, each with a written reason.
- Reads the portfolio live from Angel One, and combines technical indicators with news and earnings dates.
- Supports bot commands to run an analysis on demand, check status, and mute alerts.
- Logs every signal and later records how it played out, so the signals can be measured.

## Results and what I learned

- Before writing code, I ran a discovery process: user interviews, a hypothesis, and a scoped manual MVP plan.
- He has used the briefing every trading day for 5+ months.
- I later audited the whole system with Claude Code and fixed 15+ bugs.
- I recalibrated the signals on 155 live signals. SELL calls with no clear catalyst hit only 21% of the time and had negative alpha.
- In response I replaced a blocklist with a catalyst allowlist, added a Nifty trend filter, added signal deduplication, and tightened the RSI thresholds.
- An rsync overwrote the live signal log. I recovered 33 lost signals with a merge script. The incident write-up is in `docs/`.

## How it works

- `agent_core.py`: startup checks and the scheduler for the morning briefing, geo alerts, daily backup and outcome tracking.
- `run_briefing.py`: builds and sends the morning briefing (supports a dry run).
- `bot.py`: the Telegram bot, with its commands, the signal message format and the geo-alert loop.
- `analyst.py`: calls Claude to produce signals, validates them, and applies the catalyst allowlist and Nifty trend filter.
- `screener.py`: picks the few holdings worth a deep analysis using fixed rules, with no LLM.
- `portfolio.py`: logs in to Angel One SmartAPI and fetches the holdings.
- `technicals.py`: computes technical indicators and maps stocks to sector indices.
- `news_feed.py`: fetches general market news.
- `market_data.py`: fetches per-stock headlines and the next earnings date.
- `signal_logger.py`: writes each signal to the CSV log and handles backups.
- `outcome_tracker.py`: records prices at 1, 3, 5, 10 and 20 days after a signal, and the return against the benchmark.
- `run_tracking.py`: daily entry point that runs the outcome tracker.
- `alert_throttle.py`: limits how many alerts go out and drops near-duplicates.
- `nse_calendar.py`: NSE trading days, holidays and market hours.
- `briefing_sender.py`: sends long messages to Telegram in chunks.
- `merge_recover.py`: the one-time merge that restored the lost signals.
- `migrate_schema.py`: upgrades the signal log to the current schema without dropping data.
- `deploy.sh`: syncs code to the server and never touches runtime data.

## Tech Stack

- Python 3
- Claude Sonnet (Anthropic)
- Angel One SmartAPI
- yfinance
- NewsAPI
- python-telegram-bot
- Oracle Cloud (deployment)

## Setup

1. Clone the repo
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file based on `.env.example` and fill in your API keys (not included)
4. Run the bot:
   ```bash
   python agent_core.py
   ```

## Environment Variables

All credentials are loaded from a `.env` file which is **not included** in this repository. See `.env.example` for the required keys.
