"""Wertentwicklung des Portfolios über Zeit (aus täglichen Snapshots)."""
from datetime import date, timedelta

import pandas as pd

from core import db

# Auswählbare Zeitbereiche -> Anzahl Tage
PERIODS = {"1M": 30, "3M": 90, "6M": 180, "1J": 365, "3J": 1095, "5J": 1825}


def record_snapshots(stock_value: float, crypto_value: float, cash_value: float = 0.0, *,
                     stock_priceable: bool = True, crypto_priceable: bool = True):
    """Tages-Snapshot je Anlageklasse speichern (überschreibt denselben Tag).

    `*_priceable=False` überspringt den Snapshot dieser Klasse - ein
    kurzzeitiger Kursausfall würde sonst als echter Wertverlust im Verlauf,
    der KI-Vergleichsbasis und der Projektion landen (Positionen ohne Kurs
    zählen in *_value sonst als 0).
    """
    if stock_priceable:
        db.save_snapshot("stock", stock_value)
    if crypto_priceable:
        db.save_snapshot("crypto", crypto_value)
    db.save_snapshot("cash", cash_value)


def history_df() -> pd.DataFrame | None:
    """Snapshots als DataFrame: Index = Datum, Spalten Aktien/Krypto/Cash/Gesamt."""
    rows = db.list_snapshots()
    if not rows:
        return None
    df = pd.DataFrame(rows)
    pivot = df.pivot_table(index="snap_date", columns="asset_type",
                           values="total_value_eur", aggfunc="last")
    pivot.index = pd.to_datetime(pivot.index)
    pivot = pivot.rename(columns={"stock": "Aktien", "crypto": "Krypto", "cash": "Cash"})
    for col in ("Aktien", "Krypto", "Cash"):
        if col not in pivot:
            pivot[col] = 0.0
    pivot["Gesamt"] = (pivot["Aktien"].fillna(0) + pivot["Krypto"].fillna(0)
                       + pivot["Cash"].fillna(0))
    return pivot.sort_index()


def performance_index(history: pd.DataFrame | None = None) -> pd.DataFrame | None:
    """Bereinigter Renditeindex (Start = 100) aus Tageswerten und Kapitalflüssen.

    Einzahlungen/Auszahlungen werden als Tagesendfluss behandelt. Ohne
    hinterlegte Kapitalflüsse entspricht der Index der normalen Wertentwicklung.
    """
    history = history if history is not None else history_df()
    if history is None or len(history) < 2 or "Gesamt" not in history:
        return None
    flows = db.list_cashflows()
    flow_by_day: dict[str, float] = {}
    for flow in flows:
        flow_by_day[flow["flow_date"]] = flow_by_day.get(flow["flow_date"], 0.0) + flow["amount_eur"]

    rows = []
    index = 100.0
    previous = float(history["Gesamt"].iloc[0])
    for ts, value in history["Gesamt"].iloc[1:].items():
        current = float(value)
        flow = flow_by_day.get(ts.date().isoformat(), 0.0)
        daily_return = ((current - flow) / previous - 1.0) if previous > 0 else 0.0
        index *= 1.0 + daily_return
        rows.append({"Datum": ts, "Depotwert": current, "Kapitalfluss": flow,
                     "Rendite": daily_return, "Index": index})
        previous = current
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("Datum")


def benchmark_series(symbol: str, days: int = 3650) -> pd.Series | None:
    """Rohe Tages-Schlusskurse eines Index/Tickers über yfinance, best effort.

    Bewusst yfinance für ALLE Benchmarks (auch BTC-EUR): CoinGeckos freies
    market_chart liefert max. 365 Tage - für eine 10-Jahres-Historie unbrauchbar.
    Rohwerte statt rebasiert, damit der View selbst am ersten gemeinsamen
    Datenpunkt rebasen oder auf einer eigenen Achse in Punkten zeichnen kann.
    """
    from data import stocks as stock_data
    period = "10y" if days > 1825 else ("5y" if days > 1095 else "1y")
    try:
        prices = stock_data.get_history(symbol, period=period)
    except Exception:
        return None
    if prices is None or prices.empty or "Close" not in prices:
        return None
    close = prices["Close"].dropna()
    cutoff = pd.Timestamp(date.today() - timedelta(days=days))
    close = close[close.index >= cutoff]
    return close if len(close) >= 2 else None


def sp500_cagr(close: pd.Series | None = None) -> float | None:
    """Historische annualisierte Rendite (CAGR) des S&P 500 aus der 10J-Historie.

    Eine bereits geladene ^GSPC-Serie kann durchgereicht werden (spart den Fetch,
    wenn der S&P 500 ohnehin als Benchmark gewählt ist).
    """
    if close is None:
        close = benchmark_series("^GSPC", days=3650)
    if close is None or len(close) < 2 or float(close.iloc[0]) <= 0:
        return None
    years = (close.index[-1] - close.index[0]).days / 365.25
    if years <= 0:
        return None
    return (float(close.iloc[-1]) / float(close.iloc[0])) ** (1 / years) - 1


def projection_series(last_value: float, last_date: pd.Timestamp,
                      annual_return: float, end_year: int | None = None, *,
                      horizon_years: int | None = None,
                      monthly_contribution: float = 0.0) -> pd.Series | None:
    """Monatliche Zinseszins-Projektion ab last_date, optional mit Sparrate.

    Ziel entweder als Kalenderjahr (`end_year` -> 31.12.) oder als Horizont in
    Jahren ab last_date (`horizon_years`). `monthly_contribution` wird am Ende
    jedes Monats zum Depot addiert und mitverzinst. Startpunkt = letzter echter
    Wert, damit die Projektionslinie nahtlos an der realen Serie andockt. None,
    wenn das Ziel nicht in der Zukunft liegt oder der Ausgangswert unbrauchbar ist.
    """
    if horizon_years is not None:
        end = last_date + pd.DateOffset(years=horizon_years)
    else:
        end = pd.Timestamp(year=end_year, month=12, day=31)
    if end <= last_date or last_value <= 0:
        return None
    dates = pd.date_range(last_date, end, freq="ME")
    dates = dates[dates > last_date]
    monthly = (1 + annual_return) ** (1 / 12) - 1
    m = max(0.0, float(monthly_contribution or 0.0))
    values, v = [], last_value
    for _ in range(len(dates)):
        v = v * (1 + monthly) + m      # erst wachsen, dann Sparrate am Monatsende
        values.append(v)
    return pd.concat([pd.Series([last_value], index=[last_date]),
                      pd.Series(values, index=dates)])


def pct_from_anchor(series: pd.Series | None, anchor_date) -> pd.Series | None:
    """Serie in prozentuale Veränderung gegenüber dem Wert am Ankertag umrechnen.

    Basis = Wert am/kurz vor `anchor_date` (`asof`), damit Portfolio und Benchmark
    am selben Datum bei 0 % starten. Liegt der Anker vor dem Serienbeginn (z.B.
    junger Hebel-ETF), wird auf den ersten verfügbaren Wert rebasiert.
    """
    if series is None or series.empty:
        return None
    base = series.asof(anchor_date)
    if base is None or pd.isna(base) or float(base) <= 0:
        base = float(series.iloc[0])
    if base <= 0:
        return None
    return (series / base - 1.0) * 100.0


def snapshot_series(asset_type: str, days: int | None = None) -> pd.DataFrame | None:
    """Aufgezeichneter Wertverlauf EINER Asset-Klasse als DataFrame (Index=Datum,
    Spalte 'Wert'). `days` schneidet auf das Zeitfenster (heute − days). None wenn leer."""
    rows = [r for r in db.list_snapshots() if r["asset_type"] == asset_type]
    if days is not None:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = [r for r in rows if r["snap_date"] >= cutoff]
    if not rows:
        return None
    df = pd.DataFrame({"Wert": [r["total_value_eur"] for r in rows]},
                      index=pd.to_datetime([r["snap_date"] for r in rows]))
    return df.sort_index()


def crypto_history_series(days: int | None = None) -> tuple[pd.DataFrame | None, str]:
    """Krypto-Wertverlauf: bevorzugt die aus der Kraken-Ledger-Historie rekonstruierte
    Reihe (echte Mengen × echte Kurse), sonst die aufgezeichneten Snapshots.
    Gibt (DataFrame|None, quelle) zurück - quelle ∈ {'rekonstruiert', 'aufgezeichnet'}."""
    kraken_rows = db.list_kraken_value_history()
    if kraken_rows:
        if days is not None:
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            kraken_rows = [r for r in kraken_rows if r["snap_date"] >= cutoff]
        if kraken_rows:
            df = pd.DataFrame({"Wert": [r["value_eur"] for r in kraken_rows]},
                              index=pd.to_datetime([r["snap_date"] for r in kraken_rows]))
            return df.sort_index(), "rekonstruiert"
    return snapshot_series("crypto", days), "aufgezeichnet"
