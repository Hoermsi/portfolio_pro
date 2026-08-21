"""Markt-Sentiment für Krypto: Fear & Greed, Dominanz, Altcoin-Breite, Meme-Momentum,
Retail-Hype (App-Charts) - ausschließlich freie, keylose Quellen.

Jede Funktion ist eigenständig fehlertolerant (gibt bei jedem Problem None/{}
zurück statt zu werfen) - ein Ausfall einer Quelle darf die Krypto-Seite nie
brechen. TTL 1800s: Sentiment ändert sich langsam, und das freie CoinGecko-
Rate-Limit ist ohnehin eng (siehe data/crypto.py) - hier zusätzlich schonen.
"""
import requests

from core.cache import ttl_cache

_FNG_API = "https://api.alternative.me/fng/"
_CG_API = "https://api.coingecko.com/api/v3"
_APPLE_TOP_FREE = "https://rss.marketingtools.apple.com/api/v2/us/apps/top-free/200/apps.json"
_TTL = 1800
_TIMEOUT = 15

# Aus der "Wer schlägt BTC"-Breite ausgeschlossen: Stablecoins (an sich keine
# Kursbewegung) und Wrapped/Staked-Varianten von BTC/ETH (bilden nur den
# Basiswert nach, sind keine eigenständigen Altcoin-Wetten). Methodik angelehnt
# an den öffentlich beschriebenen Altcoin-Season-Index (blockchaincenter.net).
_BREADTH_EXCLUDED = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDE", "USDS", "PYUSD",
    "WBTC", "WSTETH", "STETH", "WEETH", "WBETH", "CBBTC", "METH",
}

# Stablecoins, deren Marktkap-Anteil zur "Stablecoin-Dominanz" aufsummiert wird.
_STABLECOIN_IDS = {"usdt", "usdc", "dai", "busd", "tusd", "fdusd", "usde", "usds", "pyusd"}


@ttl_cache(_TTL)
def fear_greed(days: int = 90) -> dict | None:
    """Crypto Fear & Greed Index (alternative.me, frei, kein Key).

    {"value": int, "classification": str, "history": [{"date": "YYYY-MM-DD", "value": int}, ...]}
    `history` ist chronologisch aufsteigend sortiert. None bei jedem Fehler.
    """
    try:
        r = requests.get(_FNG_API, params={"limit": days}, timeout=_TIMEOUT)
        r.raise_for_status()
        data = (r.json() or {}).get("data") or []
    except Exception as e:
        print(f"sentiment.fear_greed(): {e}")
        return None
    if not data:
        return None
    from datetime import datetime
    history = []
    for row in reversed(data):  # alternative.me liefert neuestes zuerst
        try:
            ts = datetime.fromtimestamp(int(row["timestamp"]))
            history.append({"date": ts.strftime("%Y-%m-%d"), "value": int(row["value"])})
        except (KeyError, ValueError, TypeError):
            continue
    if not history:
        return None
    latest = data[0]
    try:
        return {
            "value": int(latest["value"]),
            "classification": latest.get("value_classification", ""),
            "history": history,
        }
    except (KeyError, ValueError, TypeError):
        return None


@ttl_cache(_TTL)
def global_metrics() -> dict | None:
    """BTC-/ETH-/Stablecoin-Dominanz aus CoinGecko /global (frei, kein Key)."""
    try:
        r = requests.get(f"{_CG_API}/global", timeout=_TIMEOUT)
        r.raise_for_status()
        pct = ((r.json() or {}).get("data") or {}).get("market_cap_percentage") or {}
    except Exception as e:
        print(f"sentiment.global_metrics(): {e}")
        return None
    if not pct:
        return None
    stable = sum(v for k, v in pct.items() if k.lower() in _STABLECOIN_IDS)
    return {
        "btc_dominance": pct.get("btc"),
        "eth_dominance": pct.get("eth"),
        "stablecoin_dominance": round(stable, 2),
    }


@ttl_cache(_TTL)
def altcoin_breadth_30d(top_n: int = 50) -> dict | None:
    """Anteil der Top-N-Coins (ohne Stablecoins/Wrapped), die BTC über 30 Tage
    schlagen - eigene Näherung an den Altcoin-Season-Index (keine 90-Tage-API
    frei verfügbar, siehe Modul-Docstring der aufrufenden Stelle).

    {"pct_outperforming": float, "sample_size": int, "btc_change_30d": float}
    """
    try:
        r = requests.get(f"{_CG_API}/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": top_n, "page": 1, "price_change_percentage": "30d",
        }, timeout=_TIMEOUT)
        r.raise_for_status()
        coins = r.json() or []
    except Exception as e:
        print(f"sentiment.altcoin_breadth_30d(): {e}")
        return None

    btc_change = None
    changes = []
    for c in coins:
        symbol = (c.get("symbol") or "").upper()
        change = c.get("price_change_percentage_30d_in_currency")
        if symbol == "BTC":
            btc_change = change
            continue
        if symbol in _BREADTH_EXCLUDED or change is None:
            continue
        changes.append(change)

    if btc_change is None or not changes:
        return None
    beating = sum(1 for c in changes if c > btc_change)
    return {
        "pct_outperforming": round(beating / len(changes) * 100, 1),
        "sample_size": len(changes),
        "btc_change_30d": round(btc_change, 1),
    }


@ttl_cache(_TTL)
def meme_market() -> dict | None:
    """Marktkap. + 24h-Änderung der CoinGecko-Kategorie 'meme-token' (frei)."""
    try:
        r = requests.get(f"{_CG_API}/coins/categories", timeout=_TIMEOUT)
        r.raise_for_status()
        categories = r.json() or []
    except Exception as e:
        print(f"sentiment.meme_market(): {e}")
        return None
    entry = next((c for c in categories if c.get("id") == "meme-token"), None)
    if not entry:
        return None
    try:
        return {
            "market_cap": float(entry["market_cap"]),
            "change_24h_pct": float(entry["market_cap_change_24h"]),
            "volume_24h": float(entry.get("volume_24h") or 0),
        }
    except (KeyError, TypeError, ValueError):
        return None


@ttl_cache(_TTL)
def coinbase_app_rank() -> int | None:
    """Rang von Coinbase in Apples Top-200-Gratis-Apps (US-Store, frei, kein Key).

    None, wenn nicht platziert (kein Retail-Hype - das ist selbst die Aussage,
    kein Fehler)."""
    try:
        r = requests.get(_APPLE_TOP_FREE, timeout=_TIMEOUT)
        r.raise_for_status()
        results = ((r.json() or {}).get("feed") or {}).get("results") or []
    except Exception as e:
        print(f"sentiment.coinbase_app_rank(): {e}")
        return None
    for i, app in enumerate(results):
        name = f"{app.get('name', '')} {app.get('artistName', '')}".lower()
        if "coinbase" in name:
            return i + 1
    return None
