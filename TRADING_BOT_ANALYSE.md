# Analyse des Trading-Bots

Stand: 23. September 2026. Untersucht wurden insbesondere Signal-Engine, Order-Lebenszyklus, Risikoprüfungen, Runner, Hyperliquid-Anbindung und Rücktests. Die aktive Datenbank wurde ausschließlich lesend ausgewertet. Es wurden keine Handelsparameter geändert und keine Handelsaufträge ausgelöst.

## Einschätzung

Der Bot besitzt eine umfangreiche technische Grundlage, aber zwei reproduzierbare Fehler im Sicherheits- und Buchhaltungspfad. Der vorliegende Rücktest belegt die Rentabilität der aktuell betriebenen Konfiguration noch nicht belastbar. Priorität haben der Notausstieg und eine korrekte Fill-Abrechnung, danach die Übereinstimmung von Rücktest und Live-Handel.

## Tatsächliche Strategie

- Version `2.3-swing4h`: regelbasierte Long-Trendfolge auf Hyperliquid-Perpetuals; Shorts sind deaktiviert.
- BTC-Tagesregime bestimmt die Handelsrichtung. Kurs, MA20 und MA50 auf 4h-Basis bestätigen den Einstieg; zusätzlich gelten ATR-Band und Mindestliquidität.
- Der Score ordnet zulässige Kandidaten. Er ist keine Erfolgswahrscheinlichkeit.
- Anfangsstop: 2,75 × ATR, mit Grenzen. Positionsgröße folgt Risikobudget und Konzentrationslimit.
- Ab +1R wird der Stop einmal auf einen geschätzten kostenbereinigten Mindestgewinn gesetzt. Das ist kein fortlaufendes Nachziehen hinter dem Kurs.
- Ausstieg bei Gegentrend, entsprechendem MA-Signal oder Überschreiten von 336 Stunden; Strategieentscheidungen erfolgen im 4h-Takt, Sicherheitsprüfungen im 15-Minuten-Takt.
- Die KI erstellt ergänzende Berichte; die Handelsentscheidungen fallen deterministisch.

## 1. Hohe Priorität: Notausstieg kann vor seinem Aufruf abbrechen

**Fundstellen:** `core/bot.py:878`, `core/bot.py:954`, `core/bot.py:956`.

Nach einem bestätigten Einstieg mit fehlgeschlagener Stop-Platzierung fragt `open_position()` zuerst erneut den Kontostand ab, führt weitere Buchungen aus und schreibt einen Equity-Snapshot. Erst danach verarbeitet es `result.error` und versucht den Notausstieg. `_find_position()` wiederholt eine leere Antwort, fängt aber API-Ausnahmen nicht ab.

**Reproduziert mit einer simulierten Börse und SQLite im Arbeitsspeicher:** Einstieg gefüllt, Stop fehlgeschlagen, anschließende Kontostandsabfrage wirft eine Ausnahme. Ergebnis: Position an der simulierten Börse offen, **0 Notausstiegsversuche und 0 offene Positionen in der DB**. Der Runner kann den Fall später durch Reconciliation auffangen, aber die sofortige Absicherung ist ausgefallen.

**Korrektur:** Den bestätigten Fill und den fehlenden Stop unmittelbar behandeln. Ein reduce-only-Notausstieg darf nicht an einer zusätzlichen Kontostandsabfrage oder nicht wesentlichen Protokollierung scheitern. Den offenen Fill dauerhaft nachvollziehbar speichern; einen gescheiterten Notausstieg separat eskalieren und neue Einstiege sperren. Regressionstest für genau den API-Ausfall nach dem Fill ergänzen.

## 2. Mittlere Priorität: Teilschließungsgewinn wird beim Börsenabgleich doppelt gezählt

**Fundstellen:** `core/bot.py:1457`, `core/bot.py:1603`, `core/bot.py:1627`, `core/db.py:1407`.

Bot-Teilschließungen sammeln ihren Bruttogewinn in `partial_realized_pnl_usd`. Schließt die Börse später den Rest, summiert `_actual_close_from_fills()` sämtliche Schließungs-Fills seit Positionseröffnung. Darin steckt der bereits gebuchte Teilgewinn. `close_bot_position()` addiert diesen Teilgewinn anschließend erneut. Entsprechend können bereits erfasste Schließungsgebühren doppelt in die Summe gelangen.

**Reproduziert ohne Netzwerk:** Zwei Einheiten zum Einstand 100; eine Einheit zu 110 teilweise geschlossen (+10), Rest zu 120 auf der Börse geschlossen (+20), Gebühren im Beispiel null. Korrekt wären **30**, gespeichert werden **40**.

Dadurch können Gewinnstatistik und darauf aufbauende Verlustserien-Auswertungen falsch werden. Die direkt von der Börse gelesene Konto-Equity wird dadurch nicht automatisch verändert.

**Korrektur:** Fills über eindeutige IDs genau einmal verbuchen oder beim Schlussabgleich konsequent zwischen vollständigem Positions-PnL und noch nicht verbuchtem Rest-PnL unterscheiden. Beide Abschlusswege zusammen testen: Teilverkauf durch Bot, Restverkauf durch Börsen-Stop.

## 3. Rücktest entspricht nicht vollständig dem Live-Bot

Die Einschränkungen sind in `analysis/bot_backtest.py:52–74` bereits dokumentiert:

- Historischer Liquiditätsfilter wird immer als erfüllt angenommen.
- Die zusätzliche Live-Wiedereinstiegssperre nach einem abgeschlossenen Trade wird nicht simuliert.
- Das heutige Kandidatenuniversum wird auf die Vergangenheit angewendet. Historische Aufnahmebedingungen und ausgeschiedene Kandidaten sind damit nicht korrekt abgebildet.

Diese Unterschiede verändern Kandidatenwahl und Tradefolge. Ihre genaue Wirkung auf den Gewinn lässt sich nicht pauschal beziffern. Ein positiver Rücktest kann daher nicht unverändert als Nachweis für das tatsächlich ausgeführte Regelwerk dienen.

Zusätzlich beschreibt der Code Strategieänderungen anhand von Ablations- und Exit-Vergleichen. Werden Prüfzeiträume für solche Änderungen wiederverwendet, sind sie danach keine unangetasteten Testdaten mehr. Ob die dokumentierten Vergleichsläufe voneinander unabhängige Zeiträume verwendeten, wurde hier nicht nachgewiesen. Für eine belastbare Validierung sind eingefrorene Regeln und ein anschließender, zuvor ungenutzter Prüfzeitraum nötig.

## 4. Weitere Betriebsgrenze: Stop-Ersetzung wird erst im nächsten Entscheidungsfenster wiederholt

`update_trailing_stops()` hält bei fehlgeschlagener Börsen-Ersetzung den engeren lokalen Stop fest. Das ist sinnvoll. Der Aufruf erfolgt jedoch ausschließlich unter `if decision_candle` (`core/bot.py:2648–2653`). Nach einem sonst erfolgreichen Signalzyklus wird das 4h-Fenster als erledigt markiert.

Ein fehlgeschlagener Austausch wird deshalb regulär erst im nächsten 4h-Fenster erneut versucht, obwohl Kommentare den nächsten Takt versprechen. Der engere lokale Stop wird weiterhin alle 15 Minuten geprüft, der bisher bestätigte Börsen-Stop bleibt bestehen. Bei einem Runner-Ausfall gilt damit nur der ältere Börsen-Stop.

**Korrekturansatz:** Berechnung neuer Strategie-Stops im 4h-Takt belassen, Wiederholung eines bereits angeforderten, aber unbestätigten Börsen-Stops in den Sicherheitstakt aufnehmen.

## Beobachtete Konfiguration und Historie

Quelle: schreibgeschützter Auszug aus `C:/Users/Rene/AppData/Local/PortfolioPro/portfolio.db`. Die Datenbank im Projektordner enthält keine Bot-Tabellen. Die Angaben sind eine Momentaufnahme, keine unabhängige Börsenabrechnung.

- Live-Modus eingestellt (`bot_dry_run=0`), Risikostufe **10**.
- Experten-Overrides: **1 %** Risiko pro Trade, **45 %** maximale Einzelposition, **120 %** Gesamt-Exposure, **12 %** Tagesverlustgrenze, maximal **5 Positionen** und **5 gleichgerichtete Positionen**.
- Der Override für gleichgerichtete Positionen erhöht das Limit gegenüber dem Stufe-10-Standard von 3 auf 5. Bei deaktivierten Shorts können damit alle fünf Positionen gleichzeitig Long sein.
- Zuletzt gespeicherter erfolgreicher Runner-Zyklus: **23.09.2026, 15:50:58 UTC**. Das belegt einen gespeicherten Zyklus, nicht allein die aktuelle Prozessgesundheit.
- **Vier offene Bot-Positionen**, alle unter Version 2.3, aber mit unterschiedlichen Konfigurations-Hashes.
- **Zehn geschlossene Bot-Positionen** über Version 2.2 und 2.3: summierter gespeicherter realisierter PnL rund **−22,34 $**. Zusätzlich eine übernommene Position mit **−6,23 $**, getrennt zu betrachten.

Diese Beträge sind weder eine zeitgewichtete Rendite noch die vollständige Nettoperformance: offene Gewinne/Verluste und Funding sind darin nicht vollständig abgebildet; historische Betriebsmodi wurden nicht für jede Position rekonstruiert. Zudem ist die oben beschriebene Buchhaltungslücke zu berücksichtigen. Für Version 2.3 allein liegt erst eine geschlossene, vom Bot eröffnete Position vor; daraus ist keine belastbare Erfolgsquote ableitbar.

### Gespeicherte Rücktests

Der jüngste protokollierte Lauf vom **14.09.2026** verwendet vier Fenster à 45 Tage, Risikostufe 10:

| Kennzahl | Gespeicherter Wert |
|---|---:|
| Trades | 31 |
| Trefferquote | 45,16 % |
| Profit-Faktor | 3,88 |
| Durchschnittliches R-Multiple | 1,28 |
| Maximaler Rückgang | −9,21 % |
| Eigenes Prüfkriterium bestanden | Nein |

Der gespeicherte Ablehnungsgrund ist die Mindestzahl: **31 statt 40 Trades**. Die positiven Kennzahlen sind interessant, aber die Stichprobe erfüllt das eigene Kriterium nicht. Zwei ältere Läufe haben bestanden, besitzen jedoch keinen `validation_hash` und sind damit nicht eindeutig der aktuellen Konfiguration zuzuordnen. Der aktuelle Live-Schalter verlangt bewusst keinen bestandenen Rücktest.

## Verifikation und Grenzen

**437 gezielte bestehende Tests bestanden**, 42 Pandas-Deprecation-Warnungen. Geprüft wurden `test_bot`, `test_bot_guards`, `test_bot_signals`, `test_hyperliquid`, `test_bot_live`, `test_bot_live_backtest_parity`, `test_bot_backtest` und `test_bot_walkforward`.

Der erste Gesamtlauf in der Windows-Sandbox war wegen Dateizugriffsproblemen nicht als vollständiger Testnachweis verwertbar. Der gezielte Bot-Lauf außerhalb der Sandbox war erfolgreich. Die beiden Hauptfehler wurden zusätzlich mit simulierten Börsenantworten und einer isolierten In-Memory-Datenbank reproduziert. Die Ergebnisse zeigen Lücken in den bestehenden Tests, keinen Widerspruch zu deren Erfolg.

Keine neue historische Marktdatenberechnung, kein Order- oder Stop-Test gegen die echte Börse und keine Änderung am Anwendungscode. Die Priorisierung lautet: **Notausstieg absichern → Fill-Abrechnung korrigieren → Stop-Wiederholung verbessern → Rücktest an Live-Regeln angleichen → eingefrorene Strategie auf neuen Daten prüfen.**
