import os
import json
import anthropic
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from signal_logger import log_signal

load_dotenv()

CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-6"

VALID_ACTIONS = {"BUY", "SELL", "HOLD"}
VALID_CATEGORIES = {
    "earnings_surprise", "commodity_shock", "rate_decision",
    "geopolitical", "regulatory", "sector_rotation",
    "technical_only", "macro_data", "company_specific", "no_catalyst",
}

SYSTEM_PROMPT = """You are StockSage, an AI stock market analyst for NSE (Indian stock market).

Your ONLY output is a single valid JSON object. No preamble, no explanation, no markdown fences.

Follow this EXACT schema:
{
  "signals": [
    {
      "stock_symbol": "<NSE base symbol>",
      "action": "<BUY | SELL | HOLD>",
      "reason": "<max 150 chars, cite specific data>",
      "confidence_score": <integer 0-100>,
      "confidence_label": "<HIGH | MEDIUM | LOW>",
      "event_category": "<one of the allowed categories>",
      "time_horizon": "<e.g. 3-5 days>",
      "supporting_signals": {
        "rsi_14": <float or null>,
        "ma20_position": "<above | below | crossing | unknown>",
        "ma50_position": "<above | below | crossing | unknown>",
        "news_sentiment": "<positive | negative | neutral>"
      }
    }
  ],
  "market_context": "<1-2 sentence market summary>",
  "geo_watch": ["<active geopolitical risk>"]
}

Rules:
- Maximum 3 BUY or SELL signals total. HOLD signals are unlimited but only include if there is a clear reason.
- BUY requires confidence_score >= 40. SELL requires confidence_score >= 40.
- confidence_label: 70-100 = HIGH, 40-69 = MEDIUM, 0-39 = LOW.
- event_category must be exactly one of: earnings_surprise, commodity_shock, rate_decision,
  geopolitical, regulatory, sector_rotation, technical_only, macro_data, company_specific, no_catalyst.
- reason must cite specific numbers or news. Max 150 characters.
- Only include stocks from the provided portfolio.

Signal Quality Adjustments:
- sector_rotation and geopolitical event categories: Add +10 to confidence_score (these are high-conviction macro themes).
- technical_only signals must meet stricter thresholds:
  * SELL requires RSI > 75 (not 70) — avoid premature sells in trending markets.
  * BUY requires RSI < 35 (not 40) — wait for deeper oversold levels.

- Return ONLY the JSON object. Nothing else."""


def get_market_trend() -> bool:
    """
    Check if Nifty 50 (^NSEI) is in a bull market (price > 50-day MA).
    Returns True if bullish, False otherwise or on error.
    """
    try:
        end = datetime.now()
        start = end - timedelta(days=90)  # Fetch ~3 months to compute MA50

        df = yf.download(
            "^NSEI",
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
        )

        if df.empty or len(df) < 50:
            print("WARNING: get_market_trend() — insufficient data for ^NSEI")
            return False

        # Handle MultiIndex columns (yfinance >=0.2.x)
        if isinstance(df.columns, pd.MultiIndex):
            close_series = df["Close"]
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series.iloc[:, 0]
        else:
            close_series = df["Close"]

        ma50 = close_series.rolling(window=50).mean()
        current_price = float(close_series.iloc[-1])
        current_ma50 = float(ma50.iloc[-1])

        is_bullish = current_price > current_ma50
        print(f"[Market Trend] Nifty50: {current_price:.2f} vs MA50: {current_ma50:.2f} → {'BULLISH' if is_bullish else 'BEARISH'}")
        return is_bullish

    except Exception as e:
        print(f"WARNING: get_market_trend() failed — {e}")
        return False


def format_confidence_label(score):
    """Return HIGH / MEDIUM / LOW based on score."""
    if score >= 70:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


def validate_signal(signal, portfolio_symbols):
    """Return True if signal conforms to schema and references a known symbol."""
    if not isinstance(signal, dict):
        return False

    action = signal.get("action", "")
    if action not in VALID_ACTIONS:
        return False

    score = signal.get("confidence_score")
    if not isinstance(score, int) or not (0 <= score <= 100):
        return False

    if signal.get("event_category") not in VALID_CATEGORIES:
        return False

    symbol = signal.get("stock_symbol", "")
    if symbol not in portfolio_symbols:
        return False

    reason = signal.get("reason", "")
    if not reason or len(reason) > 150:
        return False

    return True


def _build_user_prompt(holdings, news, technicals):
    """Assemble the user message from the three data sources."""
    lines = ["=== PORTFOLIO HOLDINGS ==="]
    for h in holdings:
        lines.append(
            f"{h['symbol']} | qty={h['quantity']} | avg={h['avg_buy_price']} "
            f"| cmp={h['current_price']} | pnl={h['pnl_percent']:+.1f}%"
        )

    lines.append("\n=== TECHNICAL INDICATORS ===")
    for sym, t in technicals.items():
        rsi = t.get("rsi_14", "N/A")
        lines.append(
            f"{sym} | price={t['current_price']} | RSI={rsi} "
            f"| MA20={t['ma20']} ({t['ma20_position']}) "
            f"| MA50={t['ma50']} ({t['ma50_position']}) "
            f"| sector={t['sector_index_name']}@{t['sector_index_price']}"
        )
    lines.append(f"Nifty 50: {next(iter(technicals.values()))['nifty50_price']}")

    lines.append("\n=== RECENT MARKET NEWS ===")
    for i, n in enumerate(news[:10], 1):
        lines.append(f"[{i}] {n['title']} ({n['source']}, {n['published_at'][:10]})")

    lines.append(
        "\nBased on the above data, generate trading signals for this portfolio. "
        "Follow the JSON schema exactly."
    )

    return "\n".join(lines)


def _call_claude(user_prompt):
    """Call Claude API and return the raw response text."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text.strip()


def _parse_response(raw):
    """Parse Claude's raw text into a dict. Strips markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


def _fallback(error_msg):
    """Return a safe empty-signals structure with an error note."""
    return {
        "signals": [],
        "market_context": f"Analysis unavailable: {error_msg}",
        "geo_watch": [],
    }


def analyze_portfolio(holdings, news, technicals, event_type="on_demand"):
    """
    Build prompt from holdings/news/technicals, call Claude, validate and return signals.
    Retries once on invalid JSON. Returns fallback dict on persistent failure.
    """
    if not CLAUDE_API_KEY:
        print("ERROR: CLAUDE_API_KEY not set")
        return _fallback("CLAUDE_API_KEY missing")

    portfolio_symbols = {h["symbol"].split("-")[0] for h in holdings}
    user_prompt = _build_user_prompt(holdings, news, technicals)

    raw = None
    for attempt in range(1, 3):
        try:
            raw = _call_claude(user_prompt)
            data = _parse_response(raw)
            break
        except json.JSONDecodeError as e:
            print(f"WARNING: JSON parse failed (attempt {attempt}) — {e}")
            print(f"Raw response:\n{raw}\n")
            if attempt == 2:
                return _fallback(f"JSON parse error after retry: {e}")
        except Exception as e:
            print(f"ERROR: Claude API call failed — {e}")
            return _fallback(str(e))

    # Validate and fix confidence labels
    valid_signals = []
    buy_sell_count = 0

    # Check market trend once for all signals
    market_is_bullish = get_market_trend()

    for sig in data.get("signals", []):
        if not validate_signal(sig, portfolio_symbols):
            print(f"WARNING: Dropped invalid signal — {sig.get('stock_symbol')} / {sig.get('action')}")
            continue

        # Enforce max 3 BUY/SELL
        if sig["action"] in ("BUY", "SELL"):
            if buy_sell_count >= 3:
                continue
            buy_sell_count += 1

        # Enforce min confidence for BUY/SELL
        if sig["action"] in ("BUY", "SELL") and sig["confidence_score"] < 40:
            print(f"WARNING: Dropped low-confidence {sig['action']} for {sig['stock_symbol']}")
            continue

        # FIX 1: Trend Filter — suppress SELL on overbought stocks in bull market
        if sig["action"] == "SELL" and market_is_bullish:
            ss = sig.get("supporting_signals", {})
            rsi = ss.get("rsi_14")
            if rsi and rsi > 70:
                print(f"[Trend Filter] Converting SELL to HOLD for {sig['stock_symbol']} (RSI={rsi:.1f}, market bullish)")
                sig["action"] = "HOLD"
                sig["reason"] = f"Overbought (RSI {rsi:.1f}) but market bullish — wait"

        # Normalise confidence label
        sig["confidence_label"] = format_confidence_label(sig["confidence_score"])
        valid_signals.append(sig)

    data["signals"] = valid_signals

    # Log BUY/SELL signals to CSV
    signal_ids = []
    for sig in valid_signals:
        if sig["action"] in ("BUY", "SELL"):
            sym  = sig["stock_symbol"]
            tech = technicals.get(sym, {})
            try:
                sig_id = log_signal(
                    signal       = sig,
                    technicals   = technicals,
                    nifty_price  = tech.get("nifty50_price"),
                    sector_price = tech.get("sector_index_price"),
                    sector_name  = tech.get("sector_index_name", ""),
                    headlines    = news[:3],
                    event_type   = event_type,
                )
                signal_ids.append(sig_id)
            except Exception as e:
                print(f"WARNING: Failed to log signal for {sym} — {e}")

    data["signal_ids"] = signal_ids
    return data


def main():
    from portfolio import fetch_portfolio
    from news_feed import get_news
    from technicals import get_all_technicals

    print("Loading portfolio...")
    all_holdings = fetch_portfolio()
    sample = all_holdings[:5]

    print("Loading news...")
    news = get_news()

    print("Loading technicals...")
    symbols = [h["symbol"] for h in sample]
    technicals = get_all_technicals(symbols)

    print("Calling Claude for analysis...\n")
    result = analyze_portfolio(sample, news, technicals)

    # --- Print signals ---
    signals = result.get("signals", [])
    print(f"{'='*65}")
    print(f"  STOCKSAGE SIGNALS  ({len(signals)} signal(s))")
    print(f"{'='*65}")

    if not signals:
        print("  No actionable signals generated.")
    else:
        for s in signals:
            label = f"[{s['action']}]"
            conf  = f"{s['confidence_score']}% ({s['confidence_label']})"
            print(f"\n{label:8} {s['stock_symbol']}")
            print(f"  Confidence  : {conf}")
            print(f"  Category    : {s['event_category']}")
            print(f"  Horizon     : {s['time_horizon']}")
            print(f"  Reason      : {s['reason']}")
            ss = s.get("supporting_signals", {})
            print(
                f"  Signals     : RSI={ss.get('rsi_14')} | "
                f"MA20={ss.get('ma20_position')} | "
                f"MA50={ss.get('ma50_position')} | "
                f"news={ss.get('news_sentiment')}"
            )

    print(f"\n  Market Context: {result.get('market_context', 'N/A')}")
    geo = result.get("geo_watch", [])
    if geo:
        print(f"  Geo Watch     : {', '.join(geo)}")
    print(f"{'='*65}")


if __name__ == "__main__":
    main()
