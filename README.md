# StockSage v3

AI-powered stock signal bot for NSE portfolio analysis

## Features

- Morning briefing delivered at 8:30 AM IST every trading day
- Portfolio analysis using Angel One SmartAPI live data
- News sentiment analysis via NewsAPI
- Claude-powered insights and signal generation
- Telegram delivery with interactive bot commands
- Hosted on Oracle Cloud

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
