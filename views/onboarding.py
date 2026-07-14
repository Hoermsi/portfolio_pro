"""Ersteinrichtungs-Assistent: wird beim allerersten Start gezeigt (leere DB).

Fragt API-Keys und Profil ab - alles optional und später unter Einstellungen
änderbar. Setzt am Ende das Meta-Flag 'onboarded', damit der Assistent nicht
wieder erscheint.
"""
import json
from datetime import date, datetime

import streamlit as st

from core import config, db
from core.profile import max_target_return, save_risk_profile
from ui import components

_DEFAULT_TARGETS = {"stock": 60, "crypto": 20, "cash": 20}


def _finish():
    db.set_meta("onboarded", datetime.now().isoformat(timespec="seconds"))
    st.rerun()


def render():
    components.page_header(
        "Willkommen", "Ersteinrichtung",
        "Richte Portfolio Pro für dich ein. Alles ist optional und jederzeit unter "
        "Einstellungen änderbar.")

    st.markdown("#### 1 · API-Keys (optional)")
    st.caption("Ohne Anthropic-Key laufen nur die KI-Funktionen nicht - der Rest der App "
               "funktioniert normal. Kraken-Keys nur mit Leserechten (Query Funds) anlegen. "
               "Deine Keys bleiben lokal auf diesem Gerät.")
    anthropic = st.text_input("Anthropic API Key", type="password", key="onboard_anthropic",
                              help="Für die KI-Analysen. Console-Key von console.anthropic.com "
                                   "(nicht das Claude-Pro-Abo).")
    c1, c2 = st.columns(2)
    kraken_key = c1.text_input("Kraken API Key", type="password", key="onboard_kraken_key")
    kraken_secret = c2.text_input("Kraken API Secret", type="password", key="onboard_kraken_secret")

    st.divider()
    st.markdown("#### 2 · Zielallokation")
    st.caption("Orientierung fürs Dashboard - löst keine Trades aus. Standard: 60 / 20 / 20.")
    a1, a2, a3 = st.columns(3)
    stock = a1.number_input("Aktien (%)", 0, 100, _DEFAULT_TARGETS["stock"], step=5, key="onboard_stock")
    crypto = a2.number_input("Krypto (%)", 0, 100, _DEFAULT_TARGETS["crypto"], step=5, key="onboard_crypto")
    cash = a3.number_input("Cash (%)", 0, 100, _DEFAULT_TARGETS["cash"], step=5, key="onboard_cash")
    total = stock + crypto + cash
    st.caption(f"Summe: {total} %")

    st.divider()
    st.markdown("#### 3 · Risikoprofil")
    risk = st.slider("Risikobereitschaft", 1, 10, 5, key="onboard_risk")
    st.caption("1 = sehr konservativ · 5 = ausgewogen · 10 = sehr risikofreudig")
    cap = max_target_return(risk)
    tkey = "onboard_target_return"
    if tkey in st.session_state and st.session_state[tkey] > cap:
        st.session_state[tkey] = cap
    target = st.slider("Zielrendite p.a. (%)", 0.0, cap, min(6.0, cap), step=0.5, key=tkey)
    y1, y2 = st.columns(2)
    year = y1.number_input("Pensionsantritt (Jahr)", min_value=date.today().year, max_value=2100,
                           value=2060, key="onboard_year",
                           help="Startwert für den Horizont-Regler der Projektion.")
    monthly = y2.number_input("Monatliche Sparrate (€)", min_value=0.0, max_value=100000.0,
                              value=0.0, step=50.0, key="onboard_monthly")

    st.divider()
    b1, b2 = st.columns([1, 1])
    if b1.button("Einrichtung abschließen", type="primary", key="onboard_finish"):
        if total != 100:
            st.error("Die Zielallokation muss zusammen genau 100 % ergeben "
                     "(oder die Standardwerte unverändert lassen).")
        else:
            config.save_api_key("anthropic_api_key", anthropic)
            config.save_api_key("kraken_api_key", kraken_key)
            config.save_api_key("kraken_api_secret", kraken_secret)
            db.set_meta("target_allocation",
                        json.dumps({"stock": stock, "crypto": crypto, "cash": cash}))
            save_risk_profile(risk, target, int(year), monthly)
            _finish()
    if b2.button("Ohne Angaben starten", key="onboard_skip"):
        _finish()
