"""News über Google-News-RSS (kostenlos, kein Key)."""
import feedparser

from core.cache import ttl_cache


@ttl_cache(900)
def get_news(symbol: str, asset_type: str = "stock", limit: int = 5) -> list[dict]:
    query = f"{symbol}+{'crypto' if asset_type == 'crypto' else 'stock'}+news"
    url = f"https://news.google.com/rss/search?q={query}&hl=de&gl=DE&ceid=DE:de"
    items = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit]:
            items.append({
                "title": entry.title,
                "link": entry.link,
                "source": entry.source.get("title", "News") if hasattr(entry, "source") else "News",
            })
    except Exception as e:
        print(f"news.get_news({symbol}): {e}")
    return items
