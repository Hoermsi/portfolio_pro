"""Technische Analyse: Indikatoren, Fibonacci-Levels, Scoring.

Arbeitet auf jedem DataFrame mit 'Close'-Spalte (Aktien via yfinance,
Krypto via CoinGecko) - High/Low/Volume sind optional.
"""
import numpy as np
import pandas as pd


def calculate_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """RSI mit Wilder-Glättung."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Ergänzt MA20/50/200, Bollinger, RSI, MACD (und ATR falls High/Low da)."""
    df = df.copy()
    close = df["Close"]
    df["MA20"] = close.rolling(20).mean()
    df["MA50"] = close.rolling(50).mean()
    df["MA200"] = close.rolling(200).mean()
    std20 = close.rolling(20).std()
    df["Upper_Band"] = df["MA20"] + 2 * std20
    df["Lower_Band"] = df["MA20"] - 2 * std20
    df["RSI"] = calculate_rsi(close)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD"] - df["MACD_Signal"]
    if {"High", "Low"}.issubset(df.columns):
        prev_close = close.shift(1)
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        df["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def fib_levels(close: pd.Series) -> dict[str, float]:
    h, l = float(close.max()), float(close.min())
    diff = h - l
    return {
        "0%": h, "23.6%": h - 0.236 * diff, "38.2%": h - 0.382 * diff,
        "50%": h - 0.5 * diff, "61.8%": h - 0.618 * diff, "100%": l,
    }


def _last(series: pd.Series) -> float | None:
    s = series.dropna()
    return float(s.iloc[-1]) if not s.empty else None


def summarize(df: pd.DataFrame) -> dict:
    """Kennzahlen-Snapshot + technischer Score (0-100) für UI und Agenten."""
    ind = add_indicators(df)
    close = ind["Close"].dropna()
    curr = float(close.iloc[-1])
    rsi = _last(ind["RSI"]) or 50.0
    ma50 = _last(ind["MA50"])
    ma200 = _last(ind["MA200"])
    lower_b = _last(ind["Lower_Band"])
    upper_b = _last(ind["Upper_Band"])
    macd_hist = _last(ind["MACD_Hist"])
    fibs = fib_levels(close)

    # Scoring (max 100): Einstiegs-Attraktivität aus technischer Sicht
    score = 0
    if rsi < 35:
        score += 20
    elif rsi < 50:
        score += 12
    if ma200 and curr > ma200:
        score += 15
    if ma50 and ma200 and ma50 > ma200:
        score += 10
    if lower_b and curr <= lower_b * 1.03:
        score += 20
    if any(abs(curr - v) / v < 0.03 for v in fibs.values() if v > 0):
        score += 20
    if macd_hist is not None and macd_hist > 0:
        score += 15

    perf_30d = None
    if len(close) > 30:
        perf_30d = (curr / float(close.iloc[-31]) - 1) * 100

    return {
        "df": ind,
        "kurs": curr,
        "rsi": round(rsi, 1),
        "ma50": ma50,
        "ma200": ma200,
        "lower_band": lower_b,
        "upper_band": upper_b,
        "macd_hist": macd_hist,
        "fibs": fibs,
        "perf_30d_pct": round(perf_30d, 1) if perf_30d is not None else None,
        "t_score": int(min(100, max(0, score))),
    }


def summary_text(s: dict) -> str:
    """Kompakte Textdarstellung für Agenten-Prompts."""
    lines = [
        f"Kurs: {s['kurs']:.4g}",
        f"RSI(14): {s['rsi']}",
        f"MA50: {s['ma50']:.4g}" if s["ma50"] else "MA50: n/a",
        f"MA200: {s['ma200']:.4g}" if s["ma200"] else "MA200: n/a (zu wenig Historie)",
        f"Bollinger: {s['lower_band']:.4g} - {s['upper_band']:.4g}" if s["lower_band"] else "Bollinger: n/a",
        f"MACD-Histogramm: {s['macd_hist']:+.4g}" if s["macd_hist"] is not None else "MACD: n/a",
        f"Performance 30 Tage: {s['perf_30d_pct']:+.1f}%" if s["perf_30d_pct"] is not None else "",
        f"Technischer Score: {s['t_score']}/100",
    ]
    return "\n".join(f"- {ln}" for ln in lines if ln)
