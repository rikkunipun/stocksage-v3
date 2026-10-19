"""
Stage-1 screener (item 4) — pure Python, no LLM.

Scores every holding on transparent, testable flags and returns the <=5
candidates worth a deep grounded analysis. This stops the pipeline from asking
the model to rate 25 quiet stocks daily (most of the old token spend and most
of the noise), and hands the deep-dive prompt the REASON each stock was picked.

Deterministic and side-effect-free: same inputs -> same candidates. All
thresholds are module constants so they can be tuned from calibration.
"""
from dataclasses import dataclass, field

# ── Tunable thresholds ──────────────────────────────────────────────────────────
RSI_OVERSOLD    = 35     # <= is a BUY-side flag
RSI_OVERBOUGHT  = 75     # >= is a SELL-side flag (note: technical_only SELLs are
                         #   suppressed downstream per interim policy — still worth surfacing)
EARNINGS_SOON_D = 5      # earnings within N days -> surface (model will HOLD)
STRONG_SENT     = 0.40   # |sentiment| >= this is a strong-news flag
BIG_PNL_PCT     = 25.0   # |unrealized P&L %| >= this = large position swing
FAR_FROM_MA_PCT = 10.0   # price this far from MA20 = stretched
MIN_SCORE       = 2      # below this, a stock is "quiet" — excluded
MAX_CANDIDATES  = 5


@dataclass
class Candidate:
    symbol: str
    score: int = 0
    flags: list = field(default_factory=list)   # human-readable reasons it was picked
    direction_hint: str = "NEUTRAL"              # BUY | SELL | NEUTRAL — hint only, model decides
    rsi: float = None


def _score_symbol(sym, tech, earn, news) -> Candidate:
    c = Candidate(symbol=sym)
    rsi = tech.get("rsi_14") if tech else None
    c.rsi = rsi

    # RSI extremes
    if isinstance(rsi, (int, float)):
        if rsi <= RSI_OVERSOLD:
            c.score += 3; c.flags.append(f"RSI {rsi:.0f} oversold"); c.direction_hint = "BUY"
        elif rsi >= RSI_OVERBOUGHT:
            c.score += 2; c.flags.append(f"RSI {rsi:.0f} overbought"); c.direction_hint = "SELL"

    # MA cross (price sitting on a moving average — inflection)
    if tech:
        if tech.get("ma20_position") == "crossing":
            c.score += 2; c.flags.append("crossing MA20")
        if tech.get("ma50_position") == "crossing":
            c.score += 2; c.flags.append("crossing MA50")
        # stretched far from MA20
        price, ma20 = tech.get("current_price"), tech.get("ma20")
        if isinstance(price, (int, float)) and isinstance(ma20, (int, float)) and ma20:
            if abs(price - ma20) / ma20 * 100 >= FAR_FROM_MA_PCT:
                c.score += 1; c.flags.append(f"{(price-ma20)/ma20*100:+.0f}% vs MA20")

    # Earnings proximity
    if earn and earn.get("source") != "none" and earn.get("days_until") is not None:
        if 0 <= earn["days_until"] <= EARNINGS_SOON_D:
            c.score += 2; c.flags.append(f"earnings in {earn['days_until']}d")

    # News presence / strength
    hl = news or []
    if hl:
        strong = [h for h in hl if isinstance(h.get("sentiment"), (int, float))
                  and abs(h["sentiment"]) >= STRONG_SENT]
        if strong:
            s = strong[0]["sentiment"]
            c.score += 2
            c.flags.append(f"strong news (sent {s:+.2f})")
            if c.direction_hint == "NEUTRAL":
                c.direction_hint = "BUY" if s > 0 else "SELL"
        else:
            c.score += 1; c.flags.append(f"{len(hl)} recent headline(s)")

    return c


def screen(holdings, technicals, earnings=None, stock_news=None):
    """
    Return up to MAX_CANDIDATES Candidate objects, highest score first.
    Candidates below MIN_SCORE are excluded (quiet stocks).
    """
    earnings   = earnings or {}
    stock_news = stock_news or {}

    seen, cands = set(), []
    for h in holdings:
        sym = h["symbol"].split("-")[0]
        if sym in seen:
            continue
        seen.add(sym)
        # include per-holding P&L as a flag source
        c = _score_symbol(sym, technicals.get(sym), earnings.get(sym), stock_news.get(sym))
        pnl = h.get("pnl_percent")
        if isinstance(pnl, (int, float)) and abs(pnl) >= BIG_PNL_PCT:
            c.score += 2
            c.flags.append(f"P&L {pnl:+.0f}%")
            if c.direction_hint == "NEUTRAL":
                c.direction_hint = "SELL" if pnl >= BIG_PNL_PCT else "BUY"
        if c.score >= MIN_SCORE:
            cands.append(c)

    # highest score first; tie-break by RSI extremity (further from 50 first)
    cands.sort(key=lambda c: (c.score, abs((c.rsi or 50) - 50)), reverse=True)
    return cands[:MAX_CANDIDATES]


def main():
    holdings = [
        {"symbol": "DUMMY_A-EQ",   "pnl_percent": -45.0},
        {"symbol": "DUMMY_B-EQ",  "pnl_percent": +60.0},
        {"symbol": "DUMMY_C-EQ",     "pnl_percent": +2.0},
        {"symbol": "DUMMY_D-EQ",     "pnl_percent": +5.0},
        {"symbol": "DUMMY_E-EQ",    "pnl_percent": +1.0},
    ]
    tech = {
        "DUMMY_A":  {"rsi_14": 33, "ma20_position": "below", "ma50_position": "below", "current_price": 154, "ma20": 170},
        "DUMMY_B": {"rsi_14": 82, "ma20_position": "above", "ma50_position": "above", "current_price": 116, "ma20": 100},
        "DUMMY_C":    {"rsi_14": 51, "ma20_position": "above", "ma50_position": "above", "current_price": 4100, "ma20": 4080},
        "DUMMY_D":    {"rsi_14": 55, "ma20_position": "crossing", "ma50_position": "above", "current_price": 450, "ma20": 449},
        "DUMMY_E":   {"rsi_14": 49, "ma20_position": "above", "ma50_position": "above", "current_price": 95, "ma20": 94},
    }
    earn = {"DUMMY_D": {"next_earnings": "2026-08-01", "days_until": 2, "source": "yfinance"}}
    news = {"DUMMY_E": [{"title": "Sikkim tunnel collapse hits DUMMY_E project", "sentiment": -0.5, "source": "mint"}]}

    print(f"{'SYM':<8} {'SCORE':>5}  {'HINT':<8} FLAGS")
    for c in screen(holdings, tech, earn, news):
        print(f"{c.symbol:<8} {c.score:>5}  {c.direction_hint:<8} {', '.join(c.flags)}")


if __name__ == "__main__":
    main()
