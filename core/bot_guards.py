"""Trading-Bot: Sicherheits-Guards vor jeder Order.

Bewusst reine Funktionen ohne DB-/Streamlit-Import - jeder Guard bekommt
seine Eingaben explizit uebergeben (auch die Limits aus core.bot_config als
einfaches dict), statt sich Zustand selbst zu holen. Das macht jede Regel
einzeln mit einfachen Werten testbar, ohne eine Test-DB aufzusetzen, und
verhindert, dass ein Guard seine eigenen Voraussetzungen unbemerkt aendert.

Die bestehende Engine (core/shadow.py, agents/strategist.py) haelt Risiko-
regeln nur im LLM-System-Prompt - das ist fuer ein virtuelles Depot
vertretbar, bei echtem Geld nicht. Dieses Modul ist die Code-Ebene, die vor
JEDER Order laeuft, unabhaengig davon, was das Modell vorschlaegt.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class GuardResult:
    """Ergebnis einer einzelnen Pruefung. `bool(result)` ist bequemer Zugriff
    auf `.allowed`, `.reason` ist bei allowed=True immer leer."""
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


_OK = GuardResult(True)


# --- EINZELNE GUARDS ---

def check_kill_switch(kill_switch_active: bool) -> GuardResult:
    if kill_switch_active:
        return GuardResult(False, "Kill-Switch ist aktiv - keine neuen Orders, "
                                  "bis der Nutzer ihn bewusst zuruecksetzt.")
    return _OK


def check_demo_mode(live_trading_allowed: bool) -> GuardResult:
    if not live_trading_allowed:
        return GuardResult(False, "Demo-Modus aktiv - der Bot darf hier keine echte Order senden.")
    return _OK


def check_runner_health(degraded: bool) -> GuardResult:
    """`degraded` kommt aus core.bot_config.runner_degraded() - True ab
    mehreren aufeinanderfolgenden fehlgeschlagenen Zyklen. Ein Heartbeat
    allein sagt nur "der Prozess lebt", nicht "der letzte Zyklus hat
    tatsaechlich etwas getan" - ohne diesen Guard konnte ein Dauerfehler
    (z.B. eine kaputte Kerzen-Abfrage) beliebig lange unbemerkt bleiben,
    waehrend weiterhin neue Einstiege versucht wurden. Bewusst KEIN
    Kill-Switch: dieser Zustand heilt selbst, sobald wieder ein Zyklus sauber
    durchlaeuft."""
    if degraded:
        return GuardResult(False, "Mehrere Zyklen in Folge sind fehlgeschlagen - keine neuen "
                                  "Orders, bis wieder ein Zyklus sauber durchläuft.")
    return _OK


# Toleranz in PROZENTPUNKTEN fuer die Prozent-Guards. Ohne sie lehnt der Bot
# seine EIGENE, korrekt berechnete Positionsgroesse ab: bot_signals.
# position_size_usd() deckelt auf `equity * max_position_pct / 100`, der Guard
# rechnet daraus `notional / equity * 100` zurueck - und trifft je nach
# Gleitkomma-Rundung 25.000000000000004 statt 25.0. Gemessen an 2000
# Equity-Werten passierte das in rund 6 % der Faelle; im Rücktest
# (analysis/bot_backtest.py) blockierte genau diese Zeile 294 Takte am Stueck,
# ohne dass irgendwo ein Grund sichtbar geworden waere. Eine Ueberschreitung
# von 4e-15 Prozentpunkten ist keine Limitverletzung, sondern Rundung.
_PCT_EPSILON = 1e-9


def _opt_float(value) -> float | None:
    """None bleibt None (der Aufrufer entscheidet, ob das blockiert), alles
    andere wird zu float - damit ein fehlendes Limit nicht als 0.0 durchgeht."""
    return None if value is None else float(value)


def _opt_int(value) -> int | None:
    """Wie _opt_float, fuer Limits, die als ganze Zahl gelesen werden."""
    return None if value is None else int(value)


def check_order_plausibility(order_notional_usd: float, max_reasonable_notional_usd: float) -> GuardResult:
    """Faengt Kommafehler/Einheiten-Verwechslungen ab (z.B. Menge statt EUR-
    Betrag durchgereicht) - unabhaengig vom Prozent-Limit, das bei einer
    falsch berechneten Equity ebenfalls zu hoch ausfallen koennte."""
    if order_notional_usd <= 0:
        return GuardResult(False, "Order-Groesse muss positiv sein.")
    if order_notional_usd > max_reasonable_notional_usd:
        return GuardResult(False, f"Order-Groesse {order_notional_usd:.2f}$ unplausibel hoch "
                                  f"(Grenze {max_reasonable_notional_usd:.2f}$) - vermutlich ein Rechenfehler.")
    return _OK


def check_position_size(order_notional_usd: float, equity_usd: float,
                        max_position_pct: float) -> GuardResult:
    if equity_usd <= 0:
        return GuardResult(False, "Equity <= 0 - keine Positionsgroesse berechenbar.")
    pct = order_notional_usd / equity_usd * 100
    if pct > max_position_pct + _PCT_EPSILON:
        return GuardResult(False, f"Order waere {pct:.1f}% der Equity, erlaubt sind "
                                  f"max. {max_position_pct:.0f}%.")
    return _OK


def check_position_count(is_new_symbol: bool, current_open_count: int,
                         max_positions: int) -> GuardResult:
    """Nur beim Eroeffnen eines NEUEN Symbols relevant - Nachkaufen/Anpassen
    einer bestehenden Position erhoeht die Positionsanzahl nicht."""
    if is_new_symbol and current_open_count >= max_positions:
        return GuardResult(False, f"Bereits {current_open_count} offene Positionen "
                                  f"(Maximum {max_positions}).")
    return _OK


def check_leverage(requested_leverage: float, funding_rate_hourly: float | None,
                   max_leverage: float, *, side: str = "long",
                   funding_budget_hourly: float = 0.0,
                   expected_hold_hours: float = 96.0) -> GuardResult:
    """Hebel gegen ein FUNDING-KOSTENBUDGET pruefen, richtungsabhaengig.

    Die frueherre Regel "Hebel >1x nur bei Funding <= 0" hatte zwei Fehler:

    1. Sie war richtungsblind. Bei positivem Funding zahlen Longs an Shorts -
       ein Short VERDIENT dabei. Trotzdem wurde er identisch blockiert, was
       gehebelte Shorts praktisch unmoeglich machte.
    2. Sie war ein Alles-oder-Nichts-Schalter. Auf Hyperliquid ist Funding bei
       den grossen Coins fast immer leicht positiv, womit Hebel faktisch tot
       war - der Bot konnte die Einstellung nie nutzen.

    Jetzt zaehlt, was die Position ueber die geplante Haltedauer tatsaechlich
    kostet: `funding_rate_hourly * expected_hold_hours` gegen ein Budget, das
    core.bot_config aus dem Risikoregler ableitet. Die harte Kontogrenze
    `max_leverage` bleibt unberuehrt.

    DIE KOSTEN-PRUEFUNG (bekannte, zu teure Rate) GILT FUER JEDE ORDER, NICHT
    NUR >1x (Korrektur, gefunden von einer externen Pruefung 13.09.2026):
    Funding wird auf die NOMINALE gezahlt, nicht auf die hinterlegte Margin -
    eine Position mit Hebel 1 zahlt bei gleicher Nominale exakt dasselbe
    Funding wie dieselbe Nominale mit Hebel 5. Der fruehere Kurzschluss `if
    requested_leverage <= 1.0: return _OK` liess die allermeisten Orders
    (Hebel 1 ist der haeufige Fall, siehe core.bot._entry_leverage) komplett
    ungeprueft durch, obwohl genau diese Orders identisch hohe Funding-Kosten
    tragen koennen - eine bekannte, zu teure Rate blockt seither bei JEDEM
    Hebel.

    UNBEKANNTES Funding (None) blockt weiterhin NUR zusaetzlichen Hebel
    (>1x) - bewusst NICHT bei Hebel 1 verschaerft: zusaetzlicher Hebel
    vergroessert die Nominale (und damit die Fundingbelastung) pro
    eingesetzter Margin, ein unbekannter Wert ist dort riskanter zu
    ignorieren als bei Hebel 1, wo Nominale und Margin identisch sind. Ein
    "unbekannt blockiert IMMER" waere strenger als vom Review gefordert
    (der die Kosten-, nicht die Unbekannt-Pruefung an den Hebel gekoppelt
    sah) und haette in der Praxis - ein Rücktest-Fenster ohne
    Fundinghistorie, ein kurzzeitig nicht abrufbarer Live-Wert - weit mehr
    Orders blockiert, als das zugrundeliegende Risiko rechtfertigt.
    """
    if requested_leverage > max_leverage:
        return GuardResult(False, f"Hebel {requested_leverage:g}x ueberschreitet "
                                  f"das Kontolimit {max_leverage:g}x.")
    if funding_rate_hourly is None:
        if requested_leverage <= 1.0:
            return _OK
        return GuardResult(False, "Hebel >1x nur bei bekannter Funding-Rate erlaubt.")

    # Vorzeichen aus Sicht der Position: > 0 = die Position zahlt.
    cost_rate = funding_rate_hourly if side == "long" else -funding_rate_hourly
    if cost_rate <= 0:
        return _OK
    if cost_rate <= funding_budget_hourly:
        return _OK
    cost_pct = cost_rate * expected_hold_hours * 100
    budget_pct = funding_budget_hourly * expected_hold_hours * 100
    return GuardResult(False, f"Funding kostet diese {side}-Position ueber {expected_hold_hours:.0f}h "
                              f"rund {cost_pct:.2f}% - erlaubt sind {budget_pct:.2f}%.")


def check_min_notional(order_notional_usd: float, min_notional_usd: float) -> GuardResult:
    """Boersen-Mindestordergroesse (Hyperliquid: 10 $ Nominale).

    Ohne diesen Guard scheitert eine zu kleine Order erst an der API - mit
    einer Fehlermeldung, die im Protokoll wie ein technischer Ausfall
    aussieht. Bei einem bewusst kleinen Konto ist das aber der haeufigste
    reale Blocker (10 $ sind bei 45 $ Equity bereits 22 %), und er gehoert
    als solcher benannt."""
    if order_notional_usd < min_notional_usd:
        return GuardResult(False, f"Order-Groesse {order_notional_usd:.2f}$ unter der "
                                  f"Boersen-Mindestgroesse {min_notional_usd:.2f}$.")
    return _OK


def check_stop_loss_present(stop_px: float | None, side: str, entry_px: float) -> GuardResult:
    """Jede Positions-EROEFFNUNG braucht einen Stop auf der richtigen Seite
    des Einstiegs - ein Stop ueber dem Einstieg bei Long (oder umgekehrt bei
    Short) waere kein Verlustbegrenzer, sondern ein sofortiger Ausloeser."""
    if stop_px is None or stop_px <= 0:
        return GuardResult(False, "Keine Stop-Loss-Order gesetzt - Pflicht vor jeder Eroeffnung.")
    if side == "long" and stop_px >= entry_px:
        return GuardResult(False, f"Stop ({stop_px:g}) liegt nicht unter dem Long-Einstieg ({entry_px:g}).")
    if side == "short" and stop_px <= entry_px:
        return GuardResult(False, f"Stop ({stop_px:g}) liegt nicht ueber dem Short-Einstieg ({entry_px:g}).")
    return _OK


def check_symbol_cooldown(last_closed_at: datetime | None, now: datetime,
                          cooldown_hours: float) -> GuardResult:
    """Verhindert Hin-und-Her im selben Symbol direkt nach einem Ausstieg.
    Ohne vorherigen Ausstieg (last_closed_at=None) greift kein Cooldown."""
    if last_closed_at is None or cooldown_hours <= 0:
        return _OK
    elapsed_hours = (now - last_closed_at).total_seconds() / 3600
    if elapsed_hours < cooldown_hours:
        return GuardResult(False, f"Symbol-Cooldown aktiv, noch {cooldown_hours - elapsed_hours:.1f}h.")
    return _OK


def check_daily_trade_limit(trades_today: int, max_trades_per_day: int) -> GuardResult:
    if trades_today >= max_trades_per_day:
        return GuardResult(False, f"Tageslimit erreicht ({trades_today}/{max_trades_per_day} Trades).")
    return _OK


def check_daily_loss_limit(equity_now_usd: float, equity_start_of_day_usd: float,
                           daily_loss_limit_pct: float, is_new_entry: bool) -> GuardResult:
    """Sperrt nur NEUE Einstiege. Ausstiege (is_new_entry=False) bleiben immer
    erlaubt - sonst koennte der Guard genau die Verlustposition am Verkaufen
    hindern, vor der er eigentlich schuetzen soll."""
    if not is_new_entry or equity_start_of_day_usd <= 0:
        return _OK
    loss_pct = (equity_start_of_day_usd - equity_now_usd) / equity_start_of_day_usd * 100
    if loss_pct >= daily_loss_limit_pct:
        return GuardResult(False, f"Tagesverlust {loss_pct:.1f}% erreicht das Limit "
                                  f"{daily_loss_limit_pct:.0f}% - keine neuen Einstiege mehr heute.")
    return _OK


def check_equity_floor(equity_now_usd: float, equity_start_usd: float,
                       floor_pct: float) -> GuardResult:
    """Greift unabhaengig davon, ob es sich um einen Ein- oder Ausstieg
    handelt - unter dem Boden ist auch ein neuer Ausstieg (Notverkauf) noch
    erlaubt, siehe evaluate_exit_order(), das diesen Guard bewusst NICHT
    einschliesst."""
    if equity_start_usd <= 0:
        return _OK
    pct = equity_now_usd / equity_start_usd * 100
    if pct < floor_pct:
        return GuardResult(False, f"Equity bei {pct:.1f}% des Startkapitals, unter dem Boden "
                                  f"{floor_pct:.0f}% - Vollstopp, manueller Neustart noetig.")
    return _OK


def check_equity_trusted(equity_trusted: bool) -> GuardResult:
    """Blockt NUR neue Einstiege, nie Schliessungen (siehe evaluate_exit_order,
    das diesen Guard bewusst nicht einschliesst - ein Notausstieg muss immer
    moeglich bleiben, unabhaengig davon, ob die aktuelle Equity-Messung
    vertrauenswuerdig ist). Die Messung selbst kommt aus
    core.bot.equity_reading() (Boersen-Flag + Plausibilitaetspruefung gegen
    die eigene Historie) - dieser Guard prueft nur das fertige Ergebnis."""
    if not equity_trusted:
        return GuardResult(False, "Equity-Messung nicht vertrauenswürdig - keine neuen Einstiege, "
                                  "bis eine bestätigte Messung vorliegt.")
    return _OK


def check_total_exposure(order_notional_usd: float, open_notional_usd: float,
                         equity_usd: float,
                         max_total_exposure_pct: float | None) -> GuardResult:
    """Summe aller Nominalen (bereits offene Positionen + diese neue Order)
    gegen die Equity - unabhaengig von check_position_size (nur PRO Order)
    und check_position_count (nur die ANZAHL). Ohne diesen Guard sind z.B.
    fuenf Positionen zu je 41% der Equity gleichzeitig moeglich (205%
    Brutto-Exposure) - wirtschaftlich oft eine einzige, stark gehebelte
    Wette statt fuenf unabhaengiger.

    FEHLT die Grenze, wird BLOCKIERT statt durchgelassen. Vorher stand am
    Aufrufer ein Default von 999999.0 - ein fehlender oder vertippter
    Schluessel haette den Guard damit lautlos abgeschaltet, statt aufzufallen.
    Ein Guard, der bei fehlender Konfiguration durchlaesst, ist keiner."""
    if max_total_exposure_pct is None:
        return GuardResult(False, "Kein Gesamt-Exposure-Limit konfiguriert - "
                                  "keine neuen Positionen, bis eines vorliegt.")
    if equity_usd <= 0:
        return _OK
    total_pct = (open_notional_usd + order_notional_usd) / equity_usd * 100
    if total_pct > max_total_exposure_pct + _PCT_EPSILON:
        return GuardResult(False, f"Gesamt-Exposure wäre {total_pct:.0f}% der Equity, erlaubt "
                                  f"sind max. {max_total_exposure_pct:.0f}%.")
    return _OK


def check_portfolio_heat(order_heat_usd: float, open_heat_usd: float, equity_usd: float,
                         max_portfolio_heat_pct: float | None) -> GuardResult:
    """'Heat' = Dollar-Risiko bis zum jeweiligen Stop (Summe |Einstand -
    urspruenglicher Stop| * Groesse), NICHT die Nominale wie
    check_total_exposure. Zwei Depots mit identischer Brutto-Nominale koennen
    wirtschaftlich sehr unterschiedlich riskant sein, je nachdem wie eng die
    Stops stehen - check_total_exposure ist dafuer blind, weil es Stop-Abstand
    ueberhaupt nicht kennt.

    FEHLT die Grenze, wird BLOCKIERT statt durchgelassen - derselbe Grundsatz
    wie bei check_total_exposure (siehe dort): ein Guard, der bei fehlender
    Konfiguration durchlaesst, ist keiner."""
    if max_portfolio_heat_pct is None:
        return GuardResult(False, "Kein Portfolio-Heat-Limit konfiguriert - "
                                  "keine neuen Positionen, bis eines vorliegt.")
    if equity_usd <= 0:
        return _OK
    total_pct = (open_heat_usd + order_heat_usd) / equity_usd * 100
    if total_pct > max_portfolio_heat_pct + _PCT_EPSILON:
        return GuardResult(False, f"Portfolio-Heat (Dollar-Risiko bis Stop) wäre {total_pct:.2f}% "
                                  f"der Equity, erlaubt sind max. {max_portfolio_heat_pct:.2f}%.")
    return _OK


def check_same_side_concentration(is_new_symbol: bool, side: str, current_same_side_count: int,
                                   max_same_side_positions: int | None) -> GuardResult:
    """Mehrere gleichgerichtete Positionen auf verschiedenen Symbolen sind
    wirtschaftlich eine gebuendelte Richtungswette, keine unabhaengigen Wetten
    - weder check_position_count (nur die Gesamtanzahl) noch check_total_exposure
    (nur die Nominale) erfassen das. Nur beim Eroeffnen eines NEUEN Symbols
    relevant, siehe check_position_count.

    FEHLT die Grenze, wird BLOCKIERT statt durchgelassen (derselbe Grundsatz
    wie check_total_exposure/check_portfolio_heat)."""
    if not is_new_symbol:
        return _OK
    if max_same_side_positions is None:
        return GuardResult(False, "Kein Limit für gleichgerichtete Positionen konfiguriert - "
                                  "keine neuen Positionen, bis eines vorliegt.")
    if current_same_side_count >= max_same_side_positions:
        return GuardResult(False, f"Bereits {current_same_side_count} {side}-Positionen offen "
                                  f"(Maximum {max_same_side_positions} gleichgerichtet).")
    return _OK


def check_loss_streak(consecutive_losses: int, last_loss_closed_at: datetime | None,
                      now: datetime, loss_streak_limit: int) -> GuardResult:
    """Deterministischer Ersatz fuer das, was bisher implizit die KI haette
    leisten sollen (649 bot_decisions, aber nur 1 Veto). Blockt NICHT
    dauerhaft und NICHT rein streak-basiert (ein einzelner Gewinner wuerde
    sonst sofort wieder freischalten), sondern fuer volle 24 Stunden nach
    dem juengsten Verlust - ein bewusster Cooldown nach einer Verlustserie,
    kein Kalender-Artefakt kurz vor Mitternacht."""
    if loss_streak_limit <= 0 or consecutive_losses < loss_streak_limit or last_loss_closed_at is None:
        return _OK
    until = last_loss_closed_at + timedelta(hours=24)
    if now >= until:
        return _OK
    hours_left = max(0.0, (until - now).total_seconds() / 3600)
    return GuardResult(False, f"{consecutive_losses} Verlierer in Folge - keine neuen Einstiege "
                              f"für weitere {hours_left:.1f}h.")


# --- AGGREGATOREN ---

@dataclass(frozen=True)
class OrderEvaluation:
    allowed: bool
    failed_checks: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.failed_checks)

    def __bool__(self) -> bool:
        return self.allowed


def evaluate_entry_order(*, kill_switch_active: bool, live_trading_allowed: bool,
                         order_notional_usd: float, max_reasonable_notional_usd: float,
                         equity_usd: float, equity_start_usd: float,
                         equity_start_of_day_usd: float,
                         leverage: float, funding_rate_hourly: float | None,
                         stop_px: float | None, side: str, entry_px: float,
                         current_open_count: int, is_new_symbol: bool,
                         last_closed_at: datetime | None, now: datetime,
                         trades_today: int, limits: dict,
                         min_notional_usd: float = 0.0,
                         equity_trusted: bool = True,
                         open_notional_usd: float = 0.0,
                         order_heat_usd: float = 0.0,
                         open_heat_usd: float = 0.0,
                         same_side_open_count: int = 0,
                         consecutive_losses: int = 0,
                         last_loss_closed_at: datetime | None = None,
                         runner_degraded: bool = False) -> OrderEvaluation:
    """Vollstaendige Pruefkette fuer eine neue Positions-EROEFFNUNG (kaufen
    oder ein Nachkauf auf ein neues Symbol). `limits` kommt unveraendert aus
    core.bot_config.bot_limits() - hier nur als dict, damit dieses Modul
    ohne DB-Zugriff bleibt. `equity_trusted` kommt aus core.bot.equity_reading(),
    `open_notional_usd` ist die Summe der Nominalen bereits offener Positionen
    (fuer check_total_exposure) - alle neuen Parameter default-sicher (0/0/None)
    fuer Aufrufer/Tests von vor der jeweiligen Erweiterung, die diese Dimension
    nicht pruefen wollen. `runner_degraded` (core.bot_config.runner_degraded())
    ist ebenfalls default-sicher False."""
    checks = [
        check_kill_switch(kill_switch_active),
        check_demo_mode(live_trading_allowed),
        check_runner_health(runner_degraded),
        check_equity_trusted(equity_trusted),
        check_order_plausibility(order_notional_usd, max_reasonable_notional_usd),
        check_min_notional(order_notional_usd, min_notional_usd),
        check_equity_floor(equity_usd, equity_start_usd, limits["equity_floor_pct"]),
        check_daily_loss_limit(equity_usd, equity_start_of_day_usd,
                               limits["daily_loss_limit_pct"], True),
        check_daily_trade_limit(trades_today, limits["max_trades_per_day"]),
        check_position_size(order_notional_usd, equity_usd, limits["max_position_pct"]),
        check_position_count(is_new_symbol, current_open_count, limits["max_positions"]),
        check_total_exposure(order_notional_usd, open_notional_usd, equity_usd,
                             _opt_float(limits.get("max_total_exposure_pct"))),
        check_portfolio_heat(order_heat_usd, open_heat_usd, equity_usd,
                             _opt_float(limits.get("max_portfolio_heat_pct"))),
        check_same_side_concentration(is_new_symbol, side, same_side_open_count,
                                      _opt_int(limits.get("max_same_side_positions"))),
        check_loss_streak(consecutive_losses, last_loss_closed_at, now,
                          int(limits.get("loss_streak_limit", 0))),
        check_leverage(leverage, funding_rate_hourly, limits["max_leverage"], side=side,
                       funding_budget_hourly=float(limits.get("funding_budget_hourly", 0.0)),
                       expected_hold_hours=float(limits.get("max_hold_hours", 96.0))),
        check_stop_loss_present(stop_px, side, entry_px),
    ]
    if is_new_symbol:
        checks.append(check_symbol_cooldown(last_closed_at, now, limits["symbol_cooldown_hours"]))
    failed = [c.reason for c in checks if not c.allowed]
    return OrderEvaluation(allowed=not failed, failed_checks=failed)


def evaluate_adoption(*, live_trading_allowed: bool, order_notional_usd: float,
                      max_reasonable_notional_usd: float, equity_usd: float,
                      leverage: float, funding_rate_hourly: float | None,
                      stop_px: float | None, side: str, entry_px: float,
                      current_open_count: int, limits: dict,
                      equity_trusted: bool = True,
                      open_notional_usd: float = 0.0) -> OrderEvaluation:
    """Pruefkette fuer die UEBERNAHME einer auf der Boerse vorgefundenen,
    der DB unbekannten Position (core.bot.adopt_unknown_positions).

    Vorher lief die Uebernahme an JEDEM Guard vorbei: sie schrieb direkt in
    die DB. Damit konnten fuenf einzeln unauffaellige Fremdpositionen zusammen
    das Gesamt-Exposure-Limit reissen, und ein von Hand mit 20x eroeffneter
    Trade wurde mit 20x uebernommen, obwohl das Limit bei 2x liegt.

    Bewusst eine EIGENE Kette statt evaluate_entry_order, denn die Frage ist
    eine andere: nicht "soll der Bot diese Position eroeffnen?" (sie existiert
    bereits), sondern "darf der Bot sie verwalten, oder muss ein Mensch
    schauen?". Deshalb fehlen hier:

    - `check_kill_switch`: Einer bestehenden Position einen Schutz-Stop zu
      geben ist RISIKOMINDERND. Derselbe Grundsatz wie bei
      evaluate_exit_order - der Kill-Switch stoppt neue Wetten, nicht das
      Absichern vorhandener.
    - `check_daily_trade_limit` und `check_symbol_cooldown`: Taktregeln fuer
      das AUSWAEHLEN von Trades. Eine bereits offene Fremdposition
      unbeaufsichtigt zu lassen, weil das Tageskontingent erschoepft ist,
      waere die falsche Reaktion.
    - `check_min_notional`: Eine Staubposition unter der Mindestordergroesse
      ist kein Grund, den Schutz zu verweigern und den Kill-Switch zu ziehen.
    - `check_equity_floor` / `check_daily_loss_limit`: Sie wuerden genau dann
      blockieren, wenn Absichern am dringendsten ist.

    Was bleibt, sind die Fragen nach dem AGGREGATRISIKO - Groesse, Anzahl,
    Summe, Hebel - plus Demo-Modus und ein verifizierter Stop. Scheitert eine
    davon, wird NICHT uebernommen; die Position bleibt dann unbeaufsichtigt
    und OHNE Stop, weshalb der Aufrufer daraufhin den Kill-Switch zieht und
    eine manuelle Pruefung verlangt."""
    checks = [
        check_demo_mode(live_trading_allowed),
        check_equity_trusted(equity_trusted),
        check_order_plausibility(order_notional_usd, max_reasonable_notional_usd),
        check_position_size(order_notional_usd, equity_usd, limits["max_position_pct"]),
        check_position_count(True, current_open_count, limits["max_positions"]),
        check_total_exposure(order_notional_usd, open_notional_usd, equity_usd,
                             _opt_float(limits.get("max_total_exposure_pct"))),
        check_leverage(leverage, funding_rate_hourly, limits["max_leverage"], side=side,
                       funding_budget_hourly=float(limits.get("funding_budget_hourly", 0.0)),
                       expected_hold_hours=float(limits.get("max_hold_hours", 96.0))),
        check_stop_loss_present(stop_px, side, entry_px),
    ]
    failed = [c.reason for c in checks if not c.allowed]
    return OrderEvaluation(allowed=not failed, failed_checks=failed)


def evaluate_exit_order(*, live_trading_allowed: bool, order_notional_usd: float,
                        max_reasonable_notional_usd: float) -> OrderEvaluation:
    """Absichtlich nur minimal geprueft (Demo-Modus, grobe Plausibilitaet) -
    eine bestehende Position SCHLIESSEN darf nie durch ein Risikolimit
    blockiert werden, das eigentlich genau davor schuetzen soll. Das gilt
    AUCH fuer den Kill-Switch: der stoppt neue Einstiege, nicht das
    Deeskalieren bestehender Positionen - sonst koennte der Zustand, der den
    Equity-Boden ausgeloest hat (core.bot_guards.check_equity_floor), genau
    das "alles schliessen" verhindern, das als Reaktion darauf vorgesehen
    ist (core/bot.py:run_deterministic_cycle)."""
    checks = [
        check_demo_mode(live_trading_allowed),
        check_order_plausibility(order_notional_usd, max_reasonable_notional_usd),
    ]
    failed = [c.reason for c in checks if not c.allowed]
    return OrderEvaluation(allowed=not failed, failed_checks=failed)
