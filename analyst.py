import os
import json
import anthropic
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from signal_logger import log_signal

load_dotenv()

CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL   = "claude-sonnet-4-6"   # v1 path (analyze_portfolio)
DEEP_MODEL     = "claude-opus-4-8"     # v2 deep-dive (analyze_portfolio_v2)

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

GROUNDING — read carefully:
- You may ONLY cite facts that appear in the data provided below (technicals,
  the STOCK NEWS section, the EARNINGS section, market news). If a data point
  was not given to you, you do not know it.
- NEVER invent or assume: analyst calls, brokerage recommendations, price
  targets, fundamentals (P/E, ROE, growth, "strong/weak fundamentals"), or news
  events. If no news is listed for a stock, treat it as having no known news —
  do not supply any.
- Do not name a source (e.g. a newspaper or brokerage) unless that exact source
  appears in the provided headlines.
- If a stock has an earnings date within 5 days, do not issue a technical BUY or
  SELL on it — say HOLD (results pending) instead; a technical thesis can be
  invalidated overnight by the result.
- news_sentiment must reflect the provided per-stock headlines/sentiment; if
  there are none, use "neutral".

Signal Quality Adjustments:
- confidence_score must reflect your honest probability the call is right. Never
  inflate it for any category; report genuine uncertainty as a low score.
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


def _build_user_prompt(holdings, news, technicals, stock_news=None, earnings=None):
    """
    Assemble the user message. stock_news = {symbol: [headlines]} and
    earnings = {symbol: {next_earnings, days_until, source}} are optional
    grounding data (item 3); when absent the sections say so explicitly so the
    model doesn't fill the gap from priors.
    """
    stock_news = stock_news or {}
    earnings   = earnings or {}

    lines = ["=== PORTFOLIO HOLDINGS ==="]
    for h in holdings:
        lines.append(
            f"{h['symbol']} | qty={h['quantity']} | avg={h['avg_buy_price']} "
            f"| cmp={h['current_price']} | pnl={h['pnl_percent']:+.1f}%"
        )

    lines.append("\n=== TECHNICAL INDICATORS ===")
    if technicals:
        for sym, t in technicals.items():
            lines.append(
                f"{sym} | price={t.get('current_price', 'N/A')} | RSI={t.get('rsi_14', 'N/A')} "
                f"| MA20={t.get('ma20', 'N/A')} ({t.get('ma20_position', 'unknown')}) "
                f"| MA50={t.get('ma50', 'N/A')} ({t.get('ma50_position', 'unknown')}) "
                f"| sector={t.get('sector_index_name', 'N/A')}@{t.get('sector_index_price', 'N/A')}"
            )
        nifty = next(iter(technicals.values())).get("nifty50_price")
        lines.append(f"Nifty 50: {nifty if nifty is not None else 'unavailable'}")
    else:
        lines.append(
            "Technical data unavailable this run. Do NOT infer or invent indicator "
            "values; only technical_only signals are impossible today."
        )

    # Per-stock news + earnings — the grounding sections (item 3).
    lines.append("\n=== EARNINGS CALENDAR (next scheduled results) ===")
    earn_lines = []
    for sym in {h["symbol"].split("-")[0] for h in holdings}:
        e = earnings.get(sym)
        if e and e.get("source") != "none" and e.get("next_earnings"):
            within5 = e.get("days_until") is not None and e["days_until"] <= 5
            flag = "  <-- WITHIN 5 DAYS: HOLD, do not issue technical signal" if within5 else ""
            earn_lines.append(f"{sym}: {e['next_earnings']} (in {e['days_until']}d){flag}")
    lines += earn_lines if earn_lines else ["No confirmed earnings dates available (treat as unknown)."]

    lines.append("\n=== STOCK-SPECIFIC NEWS (last 7 days; cite ONLY these) ===")
    any_news = False
    for sym in {h["symbol"].split("-")[0] for h in holdings}:
        hl = stock_news.get(sym) or []
        if not hl:
            continue
        any_news = True
        lines.append(f"[{sym}]")
        for n in hl[:3]:
            sent = n.get("sentiment")
            sent_s = f", sentiment={sent:+.2f}" if isinstance(sent, (int, float)) else ""
            lines.append(f"  - {n['title']} ({n.get('source','?')}, {n.get('published_at','')[:10]}{sent_s})")
    if not any_news:
        lines.append("No stock-specific news retrieved. Treat every stock as having no known news.")

    lines.append("\n=== MARKET / MACRO NEWS ===")
    if news:
        for i, n in enumerate(news[:8], 1):
            lines.append(f"[{i}] {n['title']} ({n.get('source','?')}, {n.get('published_at','')[:10]})")
    else:
        lines.append("No market headlines available this run. Do NOT cite or invent news.")

    lines.append(
        "\nBased ONLY on the data above, generate trading signals for this portfolio. "
        "Every claim in every 'reason' must trace to a line above. Follow the JSON schema exactly."
    )

    return "\n".join(lines)


def _call_claude(user_prompt):
    """Call Claude API and return the raw response text."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,  # 1024 risked mid-JSON truncation on a 25+ holding portfolio
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

    if not holdings:
        print("WARNING: no holdings — skipping analysis")
        return _fallback("no holdings returned by Angel One")

    portfolio_symbols = {h["symbol"].split("-")[0] for h in holdings}

    # Item 3 grounding: per-stock news + earnings dates. Best-effort — if the
    # data layer is down the prompt sections say so and the model is told to
    # treat absence as "no known news", never to invent (the C4/DUMMY_F fix).
    stock_news, earnings = {}, {}
    try:
        from market_data import get_portfolio_news, get_earnings_calendar
        symbols = list(portfolio_symbols)
        stock_news = get_portfolio_news(symbols)
        earnings   = {s: get_earnings_calendar(s) for s in symbols}
    except Exception as e:
        print(f"WARNING: grounding data fetch failed — {e} (proceeding without it)")

    user_prompt = _build_user_prompt(holdings, news, technicals, stock_news, earnings)

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
    suppressed_sells = []
    buy_sell_count = 0

    # Check market trend once for all signals
    market_is_bullish = get_market_trend()

    for sig in data.get("signals", []):
        if not validate_signal(sig, portfolio_symbols):
            print(f"WARNING: Dropped invalid signal — {sig.get('stock_symbol')} / {sig.get('action')}")
            continue

        # INTERIM POLICY (calibration, Jul 2026): technical_only SELLs hit 5.9%
        # at 3d and cost -6.9% avg at 20d across Apr-May signals — suppress them
        # until signals are grounded in real data. The original SELL is shadow-
        # logged (event_type="suppressed") so the tracker keeps scoring what the
        # engine WOULD have said; re-enable only when that record turns positive.
        if sig["action"] == "SELL" and sig.get("event_category") == "technical_only":
            suppressed_sells.append(dict(sig))  # copy before demotion
            print(f"[Interim Filter] Demoting technical_only SELL → HOLD for {sig['stock_symbol']}")
            sig["action"] = "HOLD"
            sig["reason"] = f"SELL suppressed pending validation — {sig['reason']}"[:150]

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

    # Shadow-log suppressed SELLs (never sent to Telegram; scored by the
    # outcome tracker under event_type="suppressed" for calibration).
    suppressed_ids = []
    for sig in suppressed_sells:
        sym  = sig["stock_symbol"]
        tech = technicals.get(sym, {})
        try:
            sid = log_signal(
                signal       = sig,
                technicals   = technicals,
                nifty_price  = tech.get("nifty50_price"),
                sector_price = tech.get("sector_index_price"),
                sector_name  = tech.get("sector_index_name", ""),
                headlines    = news[:3],
                event_type   = "suppressed",
            )
            suppressed_ids.append(sid)
        except Exception as e:
            print(f"WARNING: Failed to shadow-log suppressed SELL for {sym} — {e}")
    data["suppressed_signal_ids"] = suppressed_ids

    return data


# ═══════════════════════════════════════════════════════════════════════════════
# ITEM 4 — Staged screener + grounded structured deep-dive (Opus 4.8)
# ═══════════════════════════════════════════════════════════════════════════════

class SupportingSignals(BaseModel):
    rsi_14: Optional[float] = None
    ma20_position: str = "unknown"     # above | below | crossing | unknown
    ma50_position: str = "unknown"
    news_sentiment: str = "neutral"    # positive | negative | neutral


class DeepSignal(BaseModel):
    stock_symbol: str
    action: str                        # BUY | SELL | HOLD
    reason: str = Field(description="<=180 chars; every claim must trace to provided data")
    confidence_score: int              # 0-100, honest probability the call is right
    event_category: str
    time_horizon: str
    entry_price: Optional[float] = None      # suggested entry (≈ current for BUY/SELL)
    stop_loss: Optional[float] = None        # price level that invalidates the thesis
    target_price: Optional[float] = None
    invalidation_condition: str = ""         # plain-language "this call is wrong if…"
    supporting_signals: SupportingSignals = Field(default_factory=SupportingSignals)


class DeepSignalReport(BaseModel):
    signals: List[DeepSignal] = Field(default_factory=list)
    market_context: str = ""
    geo_watch: List[str] = Field(default_factory=list)


V2_SYSTEM_PROMPT = """You are StockSage, a disciplined NSE (Indian market) analyst.
You are given a SHORTLIST of pre-screened holdings (already filtered by a rule
engine) plus grounding data. Analyse only these and return structured signals.

GROUNDING — absolute rules:
- Cite ONLY facts present in the data provided (technicals, STOCK NEWS, EARNINGS,
  MARKET NEWS, the stock's OWN SIGNAL HISTORY). If a fact was not given, you do
  not know it.
- NEVER invent analyst calls, brokerages, price targets, fundamentals (P/E, ROE,
  growth, "strong/weak fundamentals"), or news. Name a source only if it appears
  in the provided headlines. Absence of news = no known news.

DISCIPLINE:
- Only issue BUY/SELL when the grounding supports a specific, falsifiable thesis.
  Otherwise HOLD. Max 3 BUY/SELL total; HOLD the rest.
- If earnings are within 5 days, HOLD (results pending) — do not issue a technical
  BUY/SELL that a result could invalidate overnight.
- Consult the stock's SIGNAL HISTORY. If a very similar recent call already failed
  (e.g. a prior SELL that then rose), do not simply repeat it — either justify why
  this time differs, using new data, or step down to HOLD.
- confidence_score is your honest probability the call is right. Do not inflate.

FOR EACH BUY/SELL you MUST provide actionable, falsifiable levels:
- entry_price: sensible entry (≈ current price).
- stop_loss: the price level at which the thesis is wrong (invalidation).
- target_price: realistic objective over the stated horizon.
- invalidation_condition: one plain-language sentence — the concrete event/level
  that would prove the call wrong (e.g. "wrong if it closes below 1180 or Q1 EPS
  misses"). For HOLD, levels may be null but still state what would change your mind.
"""


def _fmt_history(hist):
    if not hist:
        return "    (no prior signals on this stock)"
    out = []
    for h in hist:
        out.append(f"    {h['date']} {h['action']} conf={h['confidence']} "
                   f"ret5d={h['return_5d']} ret20d={h['return_20d']}")
    return "\n".join(out)


def _build_v2_prompt(candidates, holdings_by_sym, technicals, stock_news, earnings,
                     market_news, histories):
    lines = ["=== SHORTLISTED CANDIDATES (analyse only these) ===\n"]
    for c in candidates:
        sym = c.symbol
        h = holdings_by_sym.get(sym, {})
        t = technicals.get(sym, {})
        e = earnings.get(sym, {})
        lines.append(f"── {sym}  (screener: {', '.join(c.flags)}; hint={c.direction_hint}) ──")
        lines.append(f"  position: qty={h.get('quantity','?')} avg={h.get('avg_buy_price','?')} "
                     f"cmp={h.get('current_price','?')} pnl={h.get('pnl_percent','?')}%")
        lines.append(f"  technicals: price={t.get('current_price','N/A')} RSI={t.get('rsi_14','N/A')} "
                     f"MA20={t.get('ma20','N/A')}({t.get('ma20_position','?')}) "
                     f"MA50={t.get('ma50','N/A')}({t.get('ma50_position','?')}) "
                     f"sector={t.get('sector_index_name','?')}@{t.get('sector_index_price','N/A')}")
        if e and e.get("source") != "none" and e.get("next_earnings"):
            lines.append(f"  earnings: {e['next_earnings']} (in {e['days_until']}d)"
                         + ("  <-- WITHIN 5 DAYS: HOLD" if (e.get('days_until') is not None and e['days_until'] <= 5) else ""))
        else:
            lines.append("  earnings: unknown (treat as unknown, do not assume)")
        news = stock_news.get(sym) or []
        if news:
            lines.append("  news:")
            for n in news[:3]:
                sent = n.get("sentiment")
                ss = f", sent={sent:+.2f}" if isinstance(sent,(int,float)) else ""
                lines.append(f"    - {n['title']} ({n.get('source','?')}, {n.get('published_at','')[:10]}{ss})")
        else:
            lines.append("  news: none retrieved (treat as no known news)")
        lines.append("  signal history (this stock's past calls + outcomes):")
        lines.append(_fmt_history(histories.get(sym)))
        lines.append("")

    lines.append("=== MARKET / MACRO NEWS ===")
    if market_news:
        for i, n in enumerate(market_news[:6], 1):
            lines.append(f"[{i}] {n['title']} ({n.get('source','?')}, {n.get('published_at','')[:10]})")
    else:
        lines.append("No market headlines this run — do not invent any.")

    lines.append("\nReturn structured signals for the candidates above, per your rules. "
                 "Every 'reason' must trace to a line above; provide entry/stop/target/"
                 "invalidation for each BUY/SELL.")
    return "\n".join(lines)


def _call_opus_structured(user_prompt):
    """
    Opus 4.8 structured deep-dive. Prefers messages.parse(output_format=...);
    falls back to messages.create + manual JSON validation if parse is
    unavailable on the installed SDK. Returns a DeepSignalReport or None.
    """
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    try:
        resp = client.messages.parse(
            model=DEEP_MODEL,
            max_tokens=3072,
            system=V2_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            output_format=DeepSignalReport,
        )
        if getattr(resp, "parsed_output", None) is not None:
            return resp.parsed_output
        # refusal or unparsed — fall through to text parse
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
        return DeepSignalReport(**_parse_response(text))
    except Exception as e:
        print(f"WARNING: structured parse failed ({e}) — falling back to plain call")
        try:
            msg = client.messages.create(
                model=DEEP_MODEL, max_tokens=3072,
                system=V2_SYSTEM_PROMPT + "\n\nReturn ONLY a JSON object matching the schema.",
                messages=[{"role": "user", "content": user_prompt}],
            )
            return DeepSignalReport(**_parse_response(msg.content[0].text.strip()))
        except Exception as e2:
            print(f"ERROR: Opus deep-dive failed — {e2}")
            return None


def analyze_portfolio_v2(holdings, market_news, technicals, event_type="on_demand"):
    """
    Item-4 pipeline: screen -> fetch grounding for candidates -> Opus 4.8
    structured deep-dive -> Python rule layer -> log. Returns a dict shaped like
    analyze_portfolio's output (signals/market_context/geo_watch/signal_ids) so
    build_signal_message works unchanged.
    """
    if not CLAUDE_API_KEY:
        return _fallback("CLAUDE_API_KEY missing")
    if not holdings:
        return _fallback("no holdings returned by Angel One")

    from screener import screen
    from signal_logger import get_signal_history

    candidates = screen(holdings, technicals or {})
    if not candidates:
        return {"signals": [], "market_context": "No candidates cleared the screener today.",
                "geo_watch": [], "signal_ids": [], "suppressed_signal_ids": []}

    cand_syms = [c.symbol for c in candidates]
    holdings_by_sym = {h["symbol"].split("-")[0]: h for h in holdings}

    # Grounding for candidates ONLY (fewer API calls than the whole portfolio).
    stock_news, earnings, histories = {}, {}, {}
    try:
        from market_data import get_stock_news, get_earnings_calendar
        for s in cand_syms:
            stock_news[s] = get_stock_news(s)
            earnings[s]   = get_earnings_calendar(s)
            histories[s]  = get_signal_history(s)
    except Exception as e:
        print(f"WARNING: v2 grounding fetch failed — {e}")
        for s in cand_syms:
            histories.setdefault(s, get_signal_history(s))

    prompt = _build_v2_prompt(candidates, holdings_by_sym, technicals or {},
                              stock_news, earnings, market_news or [], histories)

    report = _call_opus_structured(prompt)
    if report is None:
        return _fallback("Opus deep-dive returned nothing")

    portfolio_symbols = set(cand_syms) | {h["symbol"].split("-")[0] for h in holdings}
    valid, suppressed, buy_sell = [], [], 0

    for sig in report.signals:
        d = sig.model_dump()
        sym = d.get("stock_symbol", "")
        if sym not in portfolio_symbols or d.get("action") not in VALID_ACTIONS:
            print(f"WARNING: dropped v2 signal {sym}/{d.get('action')}")
            continue
        if not isinstance(d.get("confidence_score"), int) or not (0 <= d["confidence_score"] <= 100):
            continue
        if d.get("event_category") not in VALID_CATEGORIES:
            d["event_category"] = "no_catalyst"

        # Interim policy: suppress technical_only SELLs (shadow-log for calibration).
        if d["action"] == "SELL" and d["event_category"] == "technical_only":
            suppressed.append(dict(d))
            print(f"[Interim Filter] Demoting technical_only SELL → HOLD for {sym}")
            d["action"] = "HOLD"
            d["reason"] = f"SELL suppressed pending validation — {d['reason']}"[:180]

        if d["action"] in ("BUY", "SELL"):
            if buy_sell >= 3:
                d["action"] = "HOLD"
            else:
                buy_sell += 1

        d["confidence_label"] = format_confidence_label(d["confidence_score"])
        valid.append(d)

    data = {"signals": valid,
            "market_context": report.market_context,
            "geo_watch": report.geo_watch}

    signal_ids = []
    for d in valid:
        if d["action"] in ("BUY", "SELL"):
            sym = d["stock_symbol"]; tech = (technicals or {}).get(sym, {})
            try:
                sid = log_signal(signal=d, technicals=technicals or {},
                                 nifty_price=tech.get("nifty50_price"),
                                 sector_price=tech.get("sector_index_price"),
                                 sector_name=tech.get("sector_index_name", ""),
                                 headlines=stock_news.get(sym, []), event_type=event_type)
                signal_ids.append(sid)
            except Exception as e:
                print(f"WARNING: failed to log v2 signal {sym} — {e}")
    data["signal_ids"] = signal_ids

    suppressed_ids = []
    for d in suppressed:
        sym = d["stock_symbol"]; tech = (technicals or {}).get(sym, {})
        try:
            sid = log_signal(signal=d, technicals=technicals or {},
                             nifty_price=tech.get("nifty50_price"),
                             sector_price=tech.get("sector_index_price"),
                             sector_name=tech.get("sector_index_name", ""),
                             headlines=stock_news.get(sym, []), event_type="suppressed")
            suppressed_ids.append(sid)
        except Exception as e:
            print(f"WARNING: failed to shadow-log suppressed v2 SELL {sym} — {e}")
    data["suppressed_signal_ids"] = suppressed_ids
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
