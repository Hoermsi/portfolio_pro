"""Risiko-Kennzahlen für Einzelwerte und das Gesamtportfolio."""
import numpy as np
import pandas as pd

TRADING_DAYS = 252   # Aktien: Börsenhandelstage/Jahr
CRYPTO_DAYS = 365    # Krypto handelt durchgehend, auch am Wochenende


def asset_risk(df: pd.DataFrame, asset_type: str = "stock") -> dict:
    """Volatilität (annualisiert), Max Drawdown, Risikoadj. Rendite (rf=0) aus
    Tagesdaten. `asset_type="crypto"` annualisiert mit 365 statt 252 Tagen -
    mit 252 wären Krypto-Volatilität und -Sharpe systematisch zu niedrig."""
    days_per_year = CRYPTO_DAYS if asset_type == "crypto" else TRADING_DAYS
    close = df["Close"].dropna()
    if len(close) < 30:
        return {}
    returns = close.pct_change().dropna()
    vol = float(returns.std() * np.sqrt(days_per_year) * 100)
    cummax = close.cummax()
    drawdown = (close / cummax - 1.0)
    max_dd = float(drawdown.min() * 100)
    mean_annual = float(returns.mean() * days_per_year)
    sharpe = float(mean_annual / (returns.std() * np.sqrt(days_per_year) + 1e-9))
    return {
        "volatilitaet_pct": round(vol, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe": round(sharpe, 2),
    }


def concentration(values: dict[str, float]) -> dict:
    """Klumpenrisiko: größte Position, Top-3-Anteil, HHI."""
    total = sum(v for v in values.values() if v > 0)
    if total <= 0:
        return {}
    weights = sorted((v / total for v in values.values() if v > 0), reverse=True)
    hhi = sum(w * w for w in weights)
    top = max(values.items(), key=lambda kv: kv[1])
    return {
        "anzahl_positionen": len(weights),
        "groesste_position": top[0],
        "groesste_position_pct": round(weights[0] * 100, 1),
        "top3_pct": round(sum(weights[:3]) * 100, 1),
        "hhi": round(hhi, 3),  # >0.25 = stark konzentriert
    }


def correlation_matrix(histories: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    """Korrelation der Tagesrenditen mehrerer Assets."""
    series = {}
    for symbol, df in histories.items():
        if df is not None and "Close" in df:
            close = df["Close"].dropna()
            if len(close) >= 30:
                s = close.pct_change().dropna()
                s.index = s.index.normalize()
                s = s[~s.index.duplicated(keep="last")]
                series[symbol] = s
    if len(series) < 2:
        return None
    combined = pd.DataFrame(series).dropna()
    if len(combined) < 20:
        return None
    return combined.corr().round(2)
