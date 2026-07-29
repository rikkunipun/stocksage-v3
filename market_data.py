"""
Grounded market data for signal generation — item 3.

Provides real, fetched inputs to replace the model's stale/invented priors
(the C4 hallucination class, e.g. the DUMMY_F "BusinessLine ₹1271 call" that
appeared in no headline the model was given):

  get_stock_news(symbol)       -> per-stock headlines (Marketaux, paid)
  get_portfolio_news(symbols)  -> {symbol: [headlines]}, batched + cached
  get_market_news()            -> India macro headlines (Marketaux)
  get_earnings_calendar(symbol)-> next earnings date + days-until (yfinance)

Design rules:
  - Every field is read with .get(); a shape mismatch degrades to empty, never
    crashes the briefing.
  - Every returned item carries provenance ("source") and the fetched text, so
    the prompt layer can enforce "cite only what was provided".
  - Results are cached per calendar day (data/news_cache.json) so repeat runs
    and multiple /analyze calls don't burn API quota.
  - Run `python3 market_data.py probe RELIANCE` against the live API to confirm
    the real Marketaux response matches the parser before trusting it.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

MARKETAUX_KEY = os.getenv("MARKETAUX_KEY")
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
CACHE_FILE = os.path.join(DATA_DIR, "news_cache.json")

_REQUEST_TIMEOUT = 12
_MAX_HEADLINES   = 3   # free tier returns up to 3 articles/request; paid raises this


# ── Day-scoped cache ────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f:
            cache = json.load(f)
    except Exception:
        return {}
    # Drop anything not from today — news is refreshed daily.
    return {k: v for k, v in cache.items() if k.startswith(_today() + "|")}


def _save_cache(cache: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        print(f"WARNING: news cache write failed — {e}")


def _cache_key(kind: str, ident: str) -> str:
    return f"{_today()}|{kind}|{ident}"


# ── Marketaux core ──────────────────────────────────────────────────────────────

def _marketaux_request(params: dict) -> list:
    """Call Marketaux /news/all. Returns the raw `data` list, or [] on any failure."""
    if not MARKETAUX_KEY:
        print("WARNING: MARKETAUX_KEY not set — no per-stock news")
        return []
    params = {**params, "api_token": MARKETAUX_KEY, "language": "en"}
    try:
        resp = requests.get(MARKETAUX_URL, params=params, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 402 or resp.status_code == 429:
            print(f"WARNING: Marketaux quota/limit hit (HTTP {resp.status_code})")
            return []
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        print(f"WARNING: Marketaux request failed — {e}")
        return []
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    return data if isinstance(data, list) else []


def _parse_articles(raw: list, want_symbol: str = None) -> list:
    """
    Defensive parse of Marketaux articles → normalized headline dicts.
    Pulls per-entity sentiment for want_symbol when present.
    """
    out = []
    for art in raw or []:
        if not isinstance(art, dict):
            continue
        title = (art.get("title") or "").strip()
        if not title:
            continue

        sentiment = None
        if want_symbol:
            for ent in art.get("entities") or []:
                if not isinstance(ent, dict):
                    continue
                sym = (ent.get("symbol") or "").upper()
                # Marketaux NSE symbols look like "RELIANCE.NS"
                if sym.split(".")[0] == want_symbol.upper():
                    sentiment = ent.get("sentiment_score")
                    break

        out.append({
            "title":        title,
            "snippet":      (art.get("snippet") or art.get("description") or "").strip()[:280],
            "source":       art.get("source") or "Marketaux",
            "url":          art.get("url") or "",
            "published_at": art.get("published_at") or "",
            "sentiment":    sentiment,          # float [-1,1] or None
        })
    return out


# ── Public: per-stock news ──────────────────────────────────────────────────────

def get_stock_news(symbol: str, days: int = 7, limit: int = _MAX_HEADLINES) -> list:
    """
    Return up to `limit` recent headlines mentioning `symbol` (NSE).
    Cached per calendar day. Returns [] if unavailable — callers must treat an
    empty list as 'no news', never invent.
    """
    base = symbol.split("-")[0].upper()
    cache = _load_cache()
    key = _cache_key("stock", base)
    if key in cache:
        return cache[key]

    published_after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    raw = _marketaux_request({
        "symbols":         f"{base}.NS",
        "filter_entities": "true",
        "countries":       "in",
        "published_after": published_after,
        "sort":            "published_desc",
        "limit":           limit,
    })
    headlines = _parse_articles(raw, want_symbol=base)

    cache[key] = headlines
    _save_cache(cache)
    return headlines


def get_portfolio_news(symbols: list, days: int = 7) -> dict:
    """
    {base_symbol: [headlines]} for each holding. Uses the day cache; only
    un-cached symbols hit the API. A small sleep spaces out requests so a
    25-holding run doesn't trip a burst limit.
    """
    result = {}
    for i, s in enumerate(symbols):
        base = s.split("-")[0].upper()
        if base in result:
            continue
        cache = _load_cache()
        if _cache_key("stock", base) not in cache and i > 0:
            time.sleep(0.3)  # gentle spacing for un-cached fetches
        result[base] = get_stock_news(base, days=days)
    return result


# ── Public: market/macro news ───────────────────────────────────────────────────

def get_market_news(days: int = 3, limit: int = 5) -> list:
    """India market/macro headlines — replaces the GDELT+NewsAPI junk feed."""
    cache = _load_cache()
    key = _cache_key("market", "india")
    if key in cache:
        return cache[key]

    published_after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    raw = _marketaux_request({
        "countries":       "in",
        "filter_entities": "true",
        "published_after": published_after,
        "sort":            "published_desc",
        "limit":           limit,
    })
    headlines = _parse_articles(raw)
    cache[key] = headlines
    _save_cache(cache)
    return headlines


# ── Public: earnings calendar ───────────────────────────────────────────────────

def get_earnings_calendar(symbol: str) -> dict:
    """
    Next scheduled earnings date for `symbol` (NSE), via yfinance.
    Returns {"next_earnings": "YYYY-MM-DD"|None, "days_until": int|None,
             "source": "yfinance"|"none"}.
    yfinance earnings dates are patchy for midcaps — 'none' means unknown, and
    the prompt layer must treat unknown as unknown (never assume clear).
    """
    base = symbol.split("-")[0].upper()
    cache = _load_cache()
    key = _cache_key("earn", base)
    if key in cache:
        return cache[key]

    result = {"next_earnings": None, "days_until": None, "source": "none"}
    try:
        import yfinance as yf
        tk = yf.Ticker(f"{base}.NS")
        dates = tk.get_earnings_dates(limit=8)  # DataFrame indexed by datetime
        if dates is not None and not dates.empty:
            import pandas as pd
            idx = pd.to_datetime(dates.index).tz_localize(None)
            today = datetime.now()
            future = sorted(d for d in idx if d >= today - timedelta(days=1))
            if future:
                nxt = future[0]
                result = {
                    "next_earnings": nxt.strftime("%Y-%m-%d"),
                    "days_until":    (nxt.date() - today.date()).days,
                    "source":        "yfinance",
                }
    except Exception as e:
        print(f"WARNING: earnings lookup failed for {base} — {e}")

    cache[key] = result
    _save_cache(cache)
    return result


# ── CLI: probe (confirm live schema) + selftest (offline parser) ─────────────────

_SAMPLE = {
    "meta": {"found": 2, "returned": 2, "limit": 3, "page": 1},
    "data": [
        {"uuid": "a", "title": "HomeFirst Q1 profit up 20%",
         "description": "HomeFirst Finance reported...", "snippet": "HomeFirst Finance reported a 20% rise...",
         "url": "https://businessline.com/x", "published_at": "2026-07-22T06:00:00.000000Z",
         "source": "businessline.com",
         "entities": [{"symbol": "DUMMY_F.NS", "name": "Dummy Finance",
                       "exchange": "NSE", "type": "equity", "sentiment_score": 0.34}]},
        {"uuid": "b", "title": "Housing finance stocks in focus",
         "snippet": "Analysts eye housing financiers...", "url": "https://x.com/y",
         "published_at": "2026-07-21T10:00:00.000000Z", "source": "mint",
         "entities": [{"symbol": "DUMMY_F.NS", "sentiment_score": -0.1}]},
    ],
}


def _selftest():
    parsed = _parse_articles(_SAMPLE["data"], want_symbol="DUMMY_F")
    assert len(parsed) == 2, parsed
    assert parsed[0]["source"] == "businessline.com"
    assert parsed[0]["sentiment"] == 0.34
    assert parsed[1]["sentiment"] == -0.1
    # malformed / empty inputs must not crash
    assert _parse_articles([{"no_title": 1}, None, "junk"]) == []
    assert _parse_articles(None) == []
    print("selftest OK — parser handles the documented shape and malformed input")


def _probe(symbol: str):
    """Hit the live API once and print the raw shape vs what we parse."""
    base = symbol.split("-")[0].upper()
    print(f"Probing Marketaux for {base}.NS ...")
    published_after = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M")
    raw = _marketaux_request({
        "symbols": f"{base}.NS", "filter_entities": "true", "countries": "in",
        "published_after": published_after, "limit": _MAX_HEADLINES,
    })
    print(f"\nRaw articles returned: {len(raw)}")
    if raw:
        print("First raw article keys:", sorted(raw[0].keys()))
        ent = (raw[0].get('entities') or [{}])[0]
        print("First entity keys    :", sorted(ent.keys()) if ent else "none")
    print("\nParsed:")
    for h in _parse_articles(raw, want_symbol=base):
        print(f"  [{h['published_at'][:10]}] ({h['source']}, sent={h['sentiment']}) {h['title']}")
    print("\nEarnings:", get_earnings_calendar(base))
    print("\nIf 'Raw articles returned: 0' but you expect news, check the tier's")
    print("symbol format and daily quota. If keys differ from the parser, tell Claude.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "probe":
        _probe(sys.argv[2])
    else:
        _selftest()
