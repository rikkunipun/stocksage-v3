from portfolio import fetch_portfolio
from news_feed import get_news

holdings = fetch_portfolio()
news = get_news()

print(f"\nPortfolio: {len(holdings)} holdings fetched")
print(f"News: {len(news)} headlines fetched")

print("\nFirst 3 holdings:")
for h in holdings[:3]:
    print(f"  {h['symbol']}")

print("\nFirst 3 headlines:")
for n in news[:3]:
    print(f"  {n['title']}")
