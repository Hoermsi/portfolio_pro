"""Krypto-Kurse: CoinGecko (Batch, kostenlos) mit Kraken-Public-Ticker als Fallback.

Das freie CoinGecko-Rate-Limit ist eng -> alle Portfolio-Coins werden in EINEM
Request abgefragt, Erfolge werden gecacht und bei API-Fehlern wird notfalls der
letzte bekannte Kurs weiterverwendet. Coins, die CoinGecko nicht kennt oder
gerade nicht liefert, werden über den öffentlichen Kraken-Ticker bepreist.
"""
import threading
import time

import pandas as pd
import requests

from core.cache import ttl_cache

API = "https://api.coingecko.com/api/v3"
KRAKEN_PUBLIC = "https://api.kraken.com/0/public"
_TIMEOUT = 15

# Häufige Symbole -> CoinGecko-ID (spart Such-Requests)
SYMBOL_TO_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "ADA": "cardano", "DOGE": "dogecoin", "DOT": "polkadot", "LTC": "litecoin",
    "LINK": "chainlink", "AVAX": "avalanche-2", "UNI": "uniswap", "XLM": "stellar",
    "TRX": "tron", "ALGO": "algorand", "NEAR": "near", "ATOM": "cosmos",
    "MATIC": "matic-network", "POL": "polygon-ecosystem-token", "ARB": "arbitrum",
    "OP": "optimism", "AAVE": "aave", "SUI": "sui", "APT": "aptos", "INJ": "injective-protocol",
    "PEPE": "pepe", "SHIB": "shiba-inu", "FIL": "filecoin", "ETC": "ethereum-classic",
    "XMR": "monero", "ZEC": "zcash", "BCH": "bitcoin-cash", "TIA": "celestia",
    "RENDER": "render-token", "TAO": "bittensor", "KAS": "kaspa", "TON": "the-open-network",
    "USDT": "tether", "USDC": "usd-coin", "DAI": "dai", "FLR": "flare-networks",
    "GRT": "the-graph", "SAND": "the-sandbox", "MANA": "decentraland", "EGLD": "elrond-erd-2",
}

# Kraken-Ticker verwendet teils eigene Codes
_KRAKEN_TICKER_MAP = {"BTC": "XBT", "DOGE": "XDG"}

# 1:1 an EUR gebunden - nie an CoinGecko/Kraken schicken (würde falsch auflösen)
_EUR_PEGGED = {"EUR", "EURC", "EURT", "EURR"}

_PRICE_TTL = 120        # Sekunden, wie lange ein Kurs als frisch gilt
_FAIL_TTL = 300         # Sekunden, bevor eine fehlgeschlagene ID-Suche erneut versucht wird

_lock = threading.Lock()
_price_cache: dict[str, tuple[float, float]] = {}   # symbol -> (monotonic_ts, preis_eur)
_id_cache: dict[str, str] = {}                       # erfolgreiche Suchen: dauerhaft
_id_failed: dict[str, float] = {}                    # fehlgeschlagene Suchen: nur kurz merken


def _get(path: str, params: dict | None = None):
    r = requests.get(f"{API}{path}", params=params or {}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def resolve_id(symbol: str) -> str | None:
    """Symbol -> CoinGecko-ID. Erfolge werden dauerhaft gecacht, Fehlschläge
    nur kurz (damit ein Rate-Limit-Treffer den Coin nicht lange blockiert)."""
    symbol = symbol.strip().upper()
    if symbol in SYMBOL_TO_ID:
        return SYMBOL_TO_ID[symbol]
    with _lock:
        if symbol in _id_cache:
            return _id_cache[symbol]
        if time.monotonic() - _id_failed.get(symbol, float("-inf")) < _FAIL_TTL:
            return None
    try:
        res = _get("/search", {"query": symbol})
        coins = res.get("coins", [])
        exact = [c for c in coins if c.get("symbol", "").upper() == symbol]
        pick = exact[0] if exact else (coins[0] if coins else None)
    except Exception as e:
        print(f"crypto.resolve_id({symbol}): {e}")
        pick = None
    with _lock:
        if pick:
            _id_cache[symbol] = pick["id"]
            return pick["id"]
        _id_failed[symbol] = time.monotonic()
    return None


def _kraken_ticker_eur(symbol: str) -> float | None:
    """Fallback: letzter Handelskurs des EUR-Paares auf Kraken (öffentlich, kein Key)."""
    pair = _KRAKEN_TICKER_MAP.get(symbol, symbol) + "EUR"
    try:
        r = requests.get(f"{KRAKEN_PUBLIC}/Ticker", params={"pair": pair}, timeout=10)
        res = r.json()
        if res.get("error"):
            return None
        first = next(iter((res.get("result") or {}).values()), None)
        return float(first["c"][0]) if first else None
    except Exception:
        return None


def get_prices_eur(symbols) -> dict[str, float | None]:
    """Kurse für mehrere Symbole in EUR - ein einziger CoinGecko-Request für
    alles, was nicht frisch im Cache liegt; Kraken-Ticker als Fallback;
    zuletzt bekannter Kurs, wenn gerade gar nichts erreichbar ist."""
    wanted = [s.strip().upper() for s in symbols]
    now = time.monotonic()
    result: dict[str, float | None] = {}
    to_fetch: list[str] = []

    with _lock:
        for s in dict.fromkeys(wanted):
            if s in _EUR_PEGGED:
                result[s] = 1.0
                continue
            hit = _price_cache.get(s)
            if hit and now - hit[0] < _PRICE_TTL:
                result[s] = hit[1]
            else:
                to_fetch.append(s)

    if to_fetch:
        ids_map = {s: resolve_id(s) for s in to_fetch}
        ids = sorted({i for i in ids_map.values() if i})
        data = {}
        if ids:
            try:
                data = _get("/simple/price", {"ids": ",".join(ids), "vs_currencies": "eur"})
            except Exception as e:
                print(f"crypto.get_prices_eur (CoinGecko): {e}")

        for s in to_fetch:
            cid = ids_map.get(s)
            price = (data.get(cid) or {}).get("eur") if cid else None
            if price is None:
                price = _kraken_ticker_eur(s)
            if price is not None:
                with _lock:
                    _price_cache[s] = (now, float(price))
                result[s] = float(price)
            else:
                # letzter bekannter Kurs ist besser als gar keiner
                with _lock:
                    hit = _price_cache.get(s)
                result[s] = hit[1] if hit else None
    return result


def get_price_eur(symbol: str) -> float | None:
    return get_prices_eur((symbol,)).get(symbol.strip().upper())


def _kraken_ohlc_eur(symbol: str, days: int) -> pd.DataFrame | None:
    """Fallback-Historie: Kraken-Tageskerzen des EUR-Paares (max ~720 Tage)."""
    pair = _KRAKEN_TICKER_MAP.get(symbol, symbol) + "EUR"
    try:
        r = requests.get(f"{KRAKEN_PUBLIC}/OHLC",
                         params={"pair": pair, "interval": 1440}, timeout=15)
        res = r.json()
        if res.get("error"):
            return None
        rows = next((v for k, v in (res.get("result") or {}).items() if k != "last"), None)
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close",
                                         "VWAP", "Volume", "Count"])
        df.index = pd.to_datetime(df["ts"].astype(float), unit="s")
        df = df[["Open", "High", "Low", "Close"]].astype(float)
        return df.tail(days) if days else df
    except Exception:
        return None


@ttl_cache(300)
def get_history(symbol: str, days: int = 365) -> pd.DataFrame | None:
    """Tages-Historie in EUR als DataFrame mit 'Close'-Spalte (kompatibel zur TA).
    CoinGecko zuerst; bei Rate-Limit oder unbekanntem Coin Kraken-OHLC als Fallback."""
    symbol = symbol.strip().upper()
    cid = resolve_id(symbol)
    if cid:
        try:
            res = _get(f"/coins/{cid}/market_chart",
                       {"vs_currency": "eur", "days": days, "interval": "daily"})
            prices = res.get("prices", [])
            if prices:
                df = pd.DataFrame(prices, columns=["ts", "Close"])
                df.index = pd.to_datetime(df["ts"], unit="ms")
                df = df.drop(columns=["ts"])
                # letzter Punkt ist oft intraday -> auf Tagesbasis deduplizieren
                return df[~df.index.normalize().duplicated(keep="last")]
        except Exception as e:
            print(f"crypto.get_history({symbol}): {e} -> Kraken-Fallback")
    return _kraken_ohlc_eur(symbol, days)


@ttl_cache(600)
def get_market_data(symbol: str) -> dict:
    """Marktdaten für den KI-Kontext (Rang, Marktkap., Supply, ATH ...)."""
    cid = resolve_id(symbol)
    if not cid:
        return {}
    try:
        res = _get(f"/coins/{cid}", {
            "localization": "false", "tickers": "false",
            "community_data": "false", "developer_data": "false",
        })
        md = res.get("market_data", {})
        def eur(key):
            v = md.get(key)
            return v.get("eur") if isinstance(v, dict) else v
        return {
            "name": res.get("name"),
            "rang": res.get("market_cap_rank"),
            "marktkap_eur": eur("market_cap"),
            "volumen_24h_eur": eur("total_volume"),
            "kurs_eur": eur("current_price"),
            "ath_eur": eur("ath"),
            "ath_abstand_pct": (md.get("ath_change_percentage") or {}).get("eur"),
            "aenderung_24h_pct": md.get("price_change_percentage_24h"),
            "aenderung_7d_pct": md.get("price_change_percentage_7d"),
            "aenderung_30d_pct": md.get("price_change_percentage_30d"),
            "umlauf_supply": md.get("circulating_supply"),
            "max_supply": md.get("max_supply"),
        }
    except Exception as e:
        print(f"crypto.get_market_data({symbol}): {e}")
        return {}
