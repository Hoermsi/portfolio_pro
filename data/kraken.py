"""Kraken read-only API: Kontostände abrufen und als Positionen synchronisieren.

Benötigt KRAKEN_API_KEY / KRAKEN_API_SECRET in der .env (Key nur mit
'Query Funds'-Berechtigung anlegen - kein Handel, kein Withdrawal).
"""
import base64
import hashlib
import hmac
import re
import time
import urllib.parse

import requests

from core import config, db

API_URL = "https://api.kraken.com"

# Kraken-Assetcodes -> übliche Symbole
ASSET_MAP = {
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "XXRP": "XRP", "XXLM": "XLM",
    "XLTC": "LTC", "XXDG": "DOGE", "XDG": "DOGE", "XETC": "ETC", "XXMR": "XMR",
    "XZEC": "ZEC", "XREP": "REP", "XMLN": "MLN",
}
FIAT = {"ZEUR", "ZUSD", "ZGBP", "ZCAD", "ZJPY", "ZAUD", "ZCHF",
        "EUR", "USD", "GBP", "CHF", "CAD", "JPY", "AUD"}
MIN_AMOUNT = 1e-8


class KrakenError(Exception):
    pass


def _sign(urlpath: str, data: dict, secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _private(endpoint: str, data: dict | None = None) -> dict:
    key, secret = config.kraken_keys()
    if not key or not secret:
        raise KrakenError("KRAKEN_API_KEY / KRAKEN_API_SECRET fehlen in der .env")
    urlpath = f"/0/private/{endpoint}"
    data = dict(data or {})
    data["nonce"] = int(time.time() * 1000)
    headers = {"API-Key": key, "API-Sign": _sign(urlpath, data, secret)}
    r = requests.post(API_URL + urlpath, headers=headers, data=data, timeout=20)
    r.raise_for_status()
    res = r.json()
    if res.get("error"):
        raise KrakenError("; ".join(res["error"]))
    return res.get("result", {})


def normalize_asset(code: str) -> str | None:
    """Kraken-Code -> Symbol. None für Fiat."""
    base = code.split(".")[0]                    # Staking-Suffixe: ETH2.S, DOT28.S ...
    stripped = re.sub(r"\d+$", "", base)         # nur ENDziffern: ETH2 -> ETH, 1INCH bleibt
    base = stripped or base
    base = ASSET_MAP.get(base, base)
    if base in FIAT:
        return None
    return base.upper()


# --- HANDELSHISTORIE / EINSTANDSKURSE ---

# Quote-Währungen am Paarende, längere zuerst prüfen (ZEUR vor EUR, USDT vor USD)
_QUOTES = (("ZEUR", "EUR"), ("EUR", "EUR"), ("ZUSD", "USD"),
           ("USDT", "USD"), ("USDC", "USD"), ("USD", "USD"))


def split_pair(pair: str) -> tuple[str | None, str | None]:
    """Kraken-Paar -> (Basis-Code, Quote-Währung), z.B. XXBTZEUR -> (XXBT, EUR).
    Unbekannte Quotes liefern (None, None)."""
    for suffix, quote in _QUOTES:
        if pair.endswith(suffix) and len(pair) > len(suffix):
            return pair[: -len(suffix)], quote
    return None, None


def _is_rate_limit_error(err: KrakenError) -> bool:
    return "rate limit" in str(err).lower()


def _private_with_retry(endpoint: str, data: dict, max_retries: int = 5) -> dict:
    """Wie _private, aber wartet bei Kraken-Rate-Limit-Fehlern mit steigendem Backoff
    und versucht es erneut, statt die ganze Pagination abzubrechen."""
    delay = 5.0
    for attempt in range(max_retries + 1):
        try:
            return _private(endpoint, data)
        except KrakenError as e:
            if not _is_rate_limit_error(e) or attempt == max_retries:
                raise
            time.sleep(delay)
            delay = min(delay * 1.7, 30.0)


def _paginated(endpoint: str, list_key: str, page_delay: float = 3.0) -> list[dict]:
    """Holt alle Seiten eines Kraken-Endpunkts mit `ofs`-Pagination.

    TradesHistory/Ledgers kosten mehr vom privaten Rate-Limit-Zähler als andere
    Endpunkte - großzügige Pause zwischen Seiten plus Retry-mit-Backoff bei
    'Rate limit exceeded', statt bei großer Historie komplett abzubrechen.
    """
    items: list[dict] = []
    ofs = 0
    while True:
        res = _private_with_retry(endpoint, {"ofs": ofs})
        batch = res.get(list_key, {}) or {}
        if not batch:
            break
        items.extend(batch.values())
        ofs += len(batch)
        if ofs >= int(res.get("count", 0)):
            break
        time.sleep(page_delay)
    return items


def get_trades() -> list[dict]:
    """Komplette Handelshistorie (paginiert, schont das private Rate-Limit)."""
    return _paginated("TradesHistory", "trades")


def compute_avg_buy_prices(trades: list[dict], usd_to_eur: float | None) -> dict[str, float]:
    """Durchschnittlicher Kaufkurs in EUR je Symbol (nur Käufe, inkl. Gebühren).

    USD-Käufe werden näherungsweise mit dem AKTUELLEN USD/EUR-Kurs umgerechnet.
    Bestände aus Transfers/Staking tauchen nicht auf -> für die gibt es keinen Wert.
    """
    agg: dict[str, list[float]] = {}   # symbol -> [kosten_eur, volumen]
    for t in trades:
        if t.get("type") != "buy":
            continue
        base, quote = split_pair(str(t.get("pair", "")))
        if not base:
            continue
        symbol = normalize_asset(base)
        if not symbol:
            continue
        try:
            cost = float(t.get("cost", 0)) + float(t.get("fee", 0))
            vol = float(t.get("vol", 0))
        except (TypeError, ValueError):
            continue
        if vol <= 0 or cost <= 0:
            continue
        if quote == "USD":
            if not usd_to_eur:
                continue
            cost *= usd_to_eur
        entry = agg.setdefault(symbol, [0.0, 0.0])
        entry[0] += cost
        entry[1] += vol
    return {s: c / v for s, (c, v) in agg.items() if v > 0}


def average_buy_prices_eur() -> dict[str, float]:
    from data import fx
    return compute_avg_buy_prices(get_trades(), fx.get_fx_to_eur("USD"))


# --- WERTVERLAUF-REKONSTRUKTION (aus Ledger-Historie) ---

def get_ledgers() -> list[dict]:
    """Komplette Ledger-Historie (paginiert). Jeder Eintrag hat u.a. time, asset,
    amount, fee und `balance` (= resultierender Kontostand des Assets nach der Buchung)."""
    return _paginated("Ledgers", "ledger")


def daily_balances(entries: list[dict]) -> "pd.DataFrame":
    """Tagesbestände je Symbol aus Ledger-Einträgen (nutzt das `balance`-Feld direkt).

    Index = Tage (frühester Ledger-Tag … heute), Spalten = normalisierte Symbole
    (inkl. 'EUR' für Fiat-Cash), Werte = Menge am Tagesende. Staking-Varianten werden
    aufs Basissymbol summiert (XETH + ETH2.S -> ETH).
    """
    import pandas as pd

    rows = []
    for e in entries:
        try:
            ts = pd.to_datetime(float(e["time"]), unit="s")
            bal = float(e["balance"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append((ts.normalize(), str(e.get("asset", "")), bal))
    if not rows:
        return pd.DataFrame()

    raw = pd.DataFrame(rows, columns=["day", "code", "balance"])
    # letzter Stand je (Tag, Roh-Assetcode)
    raw = raw.sort_values("day").groupby(["day", "code"], as_index=False).last()
    # je Roh-Assetcode eine Spalte, auf tägliches Raster ffillen (Startbalance 0)
    wide = raw.pivot(index="day", columns="code", values="balance")
    full_idx = pd.date_range(wide.index.min(), pd.Timestamp.today().normalize(), freq="D")
    wide = wide.reindex(full_idx).ffill().fillna(0.0)

    # Roh-Codes -> Symbole mappen und summieren
    out = {}
    for code in wide.columns:
        base = code.split(".")[0].upper()
        if base in ("EUR", "ZEUR") or code.upper() in _EUR_CODES:
            symbol = "EUR"
        else:
            symbol = normalize_asset(code)
        if not symbol:
            continue   # sonstige Fiat (z.B. USD) hier ignorieren
        out[symbol] = out.get(symbol, 0.0) + wide[code]
    return pd.DataFrame(out)


def reconstruct_value_history() -> dict:
    """Rekonstruiert den täglichen EUR-Wert des Kraken-Krypto-Portfolios aus der
    Ledger-Historie × echten historischen Kursen und schreibt ihn in die DB."""
    import pandas as pd

    from data import crypto as crypto_data

    balances = daily_balances(get_ledgers())
    if balances.empty:
        return {"tage": 0, "ab_datum": None, "erfasst": [], "ohne_kurs": []}

    days_span = (pd.Timestamp.today().normalize() - balances.index.min()).days + 5
    total = pd.Series(0.0, index=balances.index)
    erfasst, ohne_kurs = [], []
    for symbol in balances.columns:
        if symbol == "EUR":
            total = total.add(balances[symbol], fill_value=0.0)   # Cash 1:1
            erfasst.append("EUR")
            continue
        hist = crypto_data.get_history(symbol, days=days_span)
        if hist is None or hist.empty:
            ohne_kurs.append(symbol)
            continue
        price = hist["Close"].copy()
        price.index = pd.to_datetime(price.index).normalize()
        price = price[~price.index.duplicated(keep="last")]
        price = price.reindex(balances.index).ffill().bfill()
        total = total.add(balances[symbol] * price, fill_value=0.0)
        erfasst.append(symbol)

    total = total[total > 0]   # führende Nullen (vor erster Buchung) weglassen
    rows = [(d.strftime("%Y-%m-%d"), float(v)) for d, v in total.items()]
    db.replace_kraken_value_history(rows)
    return {
        "tage": len(rows),
        "ab_datum": rows[0][0] if rows else None,
        "erfasst": sorted(erfasst),
        "ohne_kurs": sorted(ohne_kurs),
    }


# Kraken-Codes, die den EUR-Barbestand darstellen
_EUR_CODES = ("ZEUR", "EUR", "EUR.HOLD", "EUR.M")


def _all_balances() -> dict[str, float]:
    raw = _private_with_retry("Balance", {})
    out: dict[str, float] = {}
    for code, amount in raw.items():
        try:
            out[code] = float(amount)
        except (TypeError, ValueError):
            continue
    return out


def get_balances(raw: dict[str, float] | None = None) -> dict[str, float]:
    """Krypto-Bestände (Symbol -> Menge), Fiat und Staub herausgefiltert.
    Staking-Varianten (z.B. ETH2.S) werden aufs Basissymbol aufsummiert."""
    raw = raw if raw is not None else _all_balances()
    balances: dict[str, float] = {}
    for code, qty in raw.items():
        if qty < MIN_AMOUNT:
            continue
        symbol = normalize_asset(code)
        if not symbol:
            continue
        balances[symbol] = balances.get(symbol, 0.0) + qty
    return balances


def get_eur_cash(raw: dict[str, float] | None = None) -> float:
    """EUR-Barbestand auf Kraken (inkl. Hold-/Earn-Varianten), 1:1 in EUR."""
    raw = raw if raw is not None else _all_balances()
    total = 0.0
    for code, qty in raw.items():
        base = code.split(".")[0].upper()
        if base == "EUR" or base == "ZEUR" or code.upper() in _EUR_CODES:
            total += qty
    return total


def sync_to_db(with_cost_basis: bool = True) -> tuple[int, list[str], str | None]:
    """Bestände von Kraken in die DB übernehmen (Kategorie 'Kraken').

    with_cost_basis=True berechnet zusätzlich Durchschnitts-Kaufkurse aus der
    Handelshistorie (benötigt API-Berechtigung 'Query Closed Orders & Trades').
    Wo Kraken keinen Kaufkurs liefert (z.B. Transfers), bleibt ein vorhandener
    manueller Wert erhalten. Nicht mehr vorhandene Positionen werden entfernt.

    Gibt (anzahl, symbole, warnung_oder_None) zurück.
    """
    raw = _all_balances()
    balances = get_balances(raw)
    # EUR-Barbestand als 1:1-Cash-Position "EUR" mitführen
    eur_cash = get_eur_cash(raw)
    if eur_cash >= MIN_AMOUNT:
        balances["EUR"] = eur_cash

    avg_prices: dict[str, float] = {}
    warning = None
    if with_cost_basis:
        try:
            avg_prices = average_buy_prices_eur()
        except KrakenError as e:
            warning = (f"Einstandskurse nicht abrufbar ({e}) - fehlt dem API-Key "
                       "die Berechtigung 'Query Closed Orders & Trades'?")
        except Exception as e:
            warning = f"Einstandskurse nicht abrufbar: {e}"

    existing = {
        p.symbol: p for p in db.list_positions("crypto") if p.category == "Kraken"
    }
    synced = []
    for symbol, qty in balances.items():
        if symbol == "EUR":
            buy = 1.0                       # Cash: Einstand = Nennwert -> G/V 0
        else:
            manual = existing[symbol].buy_price_eur if symbol in existing else 0.0
            buy = avg_prices.get(symbol, manual)
        db.save_position(symbol, "crypto", qty, buy, category="Kraken", source="kraken")
        synced.append(symbol)
    for symbol, pos in existing.items():
        if symbol not in balances:
            db.delete_position(pos.id)
    return len(synced), sorted(synced), warning
