import os
import requests
from datetime import datetime, timezone
from newsapi import NewsApiClient
from dotenv import load_dotenv

load_dotenv()

NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

GDELT_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query=india%20stock%20market%20economy"
    "&mode=artlist&maxrecords=10&format=json"
)


def _parse_dt(dt_str):
    """Parse a datetime string into a UTC-aware datetime. Returns None on failure."""
    if not dt_str:
        return None
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(dt_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def fetch_gdelt():
    """Fetch headlines from GDELT v2. Returns list of headline dicts."""
    try:
        resp = requests.get(GDELT_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"WARNING: GDELT fetch failed — {e}")
        return []

    articles = data.get("articles") or []
    headlines = []

    for item in articles:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        headlines.append({
            "title": title,
            "source": item.get("domain") or "GDELT",
            "url": item.get("url") or "",
            "published_at": item.get("seendate") or "",
        })

    return headlines


def fetch_newsapi():
    """Fetch headlines from NewsAPI. Returns list of headline dicts."""
    if not NEWSAPI_KEY:
        print("WARNING: NEWSAPI_KEY not set — skipping NewsAPI")
        return []

    try:
        client = NewsApiClient(api_key=NEWSAPI_KEY)
        response = client.get_everything(
            q="India stock market OR NSE OR Nifty OR RBI",
            language="en",
            sort_by="publishedAt",
            page_size=10,
        )
    except Exception as e:
        print(f"WARNING: NewsAPI fetch failed — {e}")
        return []

    if response.get("status") != "ok":
        print(f"WARNING: NewsAPI returned status '{response.get('status')}'")
        return []

    headlines = []
    for item in response.get("articles") or []:
        title = (item.get("title") or "").strip()
        if not title or title == "[Removed]":
            continue
        source = ""
        if isinstance(item.get("source"), dict):
            source = item["source"].get("name") or ""
        headlines.append({
            "title": title,
            "source": source or "NewsAPI",
            "url": item.get("url") or "",
            "published_at": item.get("publishedAt") or "",
        })

    return headlines


def get_news():
    """
    Fetch, merge, deduplicate, and return top 10 most recent headlines
    from GDELT and NewsAPI combined.
    """
    gdelt = fetch_gdelt()
    newsapi = fetch_newsapi()

    if not gdelt and not newsapi:
        print("WARNING: Both news sources failed. No headlines available.")
        return []

    combined = gdelt + newsapi

    # Deduplicate by title (case-insensitive)
    seen = set()
    unique = []
    for h in combined:
        key = h["title"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(h)

    # Sort by published_at descending; unparseable dates go to the end
    def sort_key(h):
        dt = _parse_dt(h["published_at"])
        return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)

    unique.sort(key=sort_key, reverse=True)

    return unique[:10]


def main():
    headlines = get_news()

    if not headlines:
        print("No headlines retrieved.")
        return

    print(f"\n{'='*70}")
    print(f"  MARKET NEWS  ({len(headlines)} headlines)")
    print(f"{'='*70}")

    for i, h in enumerate(headlines, 1):
        print(f"\n[{i}] {h['title']}")
        print(f"    Source : {h['source']}")
        print(f"    Date   : {h['published_at']}")
        print(f"    URL    : {h['url']}")

    print(f"\n{'='*70}")


if __name__ == "__main__":
    main()
