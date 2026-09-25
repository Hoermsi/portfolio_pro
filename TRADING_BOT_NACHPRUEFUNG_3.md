# Dritte Prüfung der Trading-Bot-Befunde

## Aktualisierung nach erneuter Prüfung

Der unten dokumentierte P1-Befund zur teilweisen Notfall-Schließung ist nach der aktuellen Korrektur **in den nachgestellten Szenarien behoben**. Der ursprüngliche Abschnitt bleibt als historische Fehlerbeschreibung erhalten.

Aktueller gezielter Testlauf: **456 Tests bestanden**, 44 Pandas-Deprecation-Warnungen, 81,52 Sekunden. Zwei neue Regressionstests behandeln den teilweisen Notausstieg mit funktionierender beziehungsweise ausgefallener DB.

Unabhängig mit simuliertem Exchange und SQLite im Arbeitsspeicher geprüft:

- Einstieg 0,12 Einheiten, Notausstieg schließt 0,06: Restmenge 0,06 bleibt korrekt erfasst; keine Vollschlussmeldung; Wiederholungsmarker und Kill-Switch sind gesetzt.
- Nächster Sicherheitscheck bei einem Kurs oberhalb des Long-Stops: Der Wiederholungsmarker löst trotzdem die Restschließung aus. Anschließend ist die Position geschlossen, der Marker gelöscht und der Kill-Switch weiterhin aktiv.
- Vollständiger DB-Ausfall unmittelbar nach dem Einstieg: Der Notausstieg wird versucht und die Teilausführung anhand der Fill-Menge korrekt erkannt. Die Rückmeldung enthält sowohl den fehlgeschlagenen Notausstieg als auch die Buchungsfehler. Bei nicht verfügbarer DB können Marker und Kill-Switch nicht persistent gespeichert werden; das wurde nicht als erfolgreiche Speicherung gewertet.

Im gezielt geprüften Korrekturpfad kein weiterer Befund. Keine Änderung am Anwendungscode oder an Handelsparametern und keine echte Order. Dies ist eine Prüfung des beschriebenen Fehlerpfads, keine vollständige Freigabe aller Bot-Funktionen.

---

Stand: 23.09.2026. Geprüft wurden die Korrekturen zu `TRADING_BOT_REANALYSE.md`, ihre unmittelbaren Fehlerpfade und die zuvor genannten Backtest-Abweichungen. Keine Änderung an Anwendungscode oder Handelsparametern; keine echten Orders, keine erneute Auswertung der aktiven Handelsdatenbank.

## Ergebnis

Die zuletzt konkret nachgewiesenen Fehler sind in den erneut getesteten Szenarien behoben. Im umgebauten Notausstiegspfad besteht jedoch eine weitere sicherheitsrelevante Lücke bei Teilausführungen.

| Erneut geprüfter Fall | Ergebnis |
|---|---|
| Kontostandsabfrage fällt nach Einstieg aus | Schließungsversuch erfolgt, simulierte Position geschlossen |
| Kontostands- und Kursabfrage fallen aus | Schließungsversuch erfolgt, simulierte Position geschlossen |
| Order-Buchung schlägt nach Einstieg fehl | Schließungsversuch erfolgt vor der Buchhaltung; simulierte Position geschlossen, Buchungsfehler sichtbar gemeldet |
| Teil-Fill innerhalb derselben Sekunde | Korrekt 30 statt früher 40 Dollar |
| Gleicher Fall mit Gebühren | Korrekt 28,60 Dollar netto |
| Restschluss liegt vor verzögerter lokaler Buchung | Korrekt 28,60 Dollar netto |

Die Notfall-Schließung läuft nun über `_emergency_flatten()` unmittelbar nach dem bestätigten Einstieg ohne Stop. Die Abrechnung verwendet die noch offene Restmenge zur Auswahl der Schluss-Fills statt des lokalen Buchungszeitpunkts. Die obigen Ergebnisse wurden unabhängig von den vorhandenen Tests mit simulierten Börsenantworten und SQLite im Arbeitsspeicher nachgestellt.

## P1 – Teilweise gefüllter Notausstieg wird als vollständige Schließung behandelt

**Fundstelle:** `core/bot.py:833–847`, insbesondere der ignorierte Rückgabewert von `_book_confirmed_close()`.

`flat.status == 'filled'` bedeutet, dass die Schließungsorder eine Ausführung erhalten hat. Es bedeutet nicht zwingend, dass die gesamte Position geschlossen wurde. Das ist im regulären Schließungspfad bereits berücksichtigt: `_book_confirmed_close()` vergleicht die ausgeführte Menge und gibt bei einer Restposition `status='partial'` zurück.

Im neuen Notfallpfad ruft `_book_close()` diese Funktion jedoch ohne Weitergabe oder Auswertung ihres Rückgabewerts auf. Der äußere Zweig meldet anschließend uneingeschränkt „Position aus Sicherheitsgründen sofort geschlossen“. Die Eskalation für einen fehlgeschlagenen Notausstieg wird dabei übersprungen.

**Unabhängige Reproduktion:**

1. Einstieg: 0,12 Einheiten, Stop-Platzierung fehlgeschlagen.
2. Notausstieg: `status='filled'`, aber nur 0,06 Einheiten ausgeführt.
3. Simulierte Börse und Datenbank behalten 0,06 Einheiten offen.
4. Rückmeldung behauptet vollständige Schließung.
5. `bot_stop_retry:BTC` ist nicht gesetzt; Kill-Switch bleibt aus.

Die Restposition ist weiterhin ohne bestätigten Börsen-Stop. Ein lokaler Stop kann später greifen, beseitigt aber weder die fehlende sofortige Restschließung noch die falsche Erfolgsmeldung. Auch die spezielle Runner-Benachrichtigung für einen fehlgeschlagenen Notausstieg wird durch diese Rückmeldung nicht ausgelöst.

**Korrekturansatz:** Den tatsächlich geschlossenen Anteil gegenüber dem offenen Bestand prüfen und das Buchungsergebnis weitergeben. Bei Restmenge darf der Vollschluss-Zweig nicht ausgeführt werden. Restbestand dokumentieren, erneuten reduce-only-Schließungsversuch ermöglichen, Wiederholungsmarker und Kill-Switch setzen sowie die ungeschützte Restposition klar melden. Die Sicherheitsentscheidung sollte auch bei ausfallender Buchhaltung anhand der verfügbaren Ausführungsdaten möglich bleiben.

**Fehlender Regressionstest:** Stop-Platzierung scheitert, Notausstieg schließt nur einen Teil. Erwartet: keine Vollschlussmeldung, Restmenge erhalten, Wiederholungsmarker gesetzt und neue Einstiege gesperrt. Zusätzlich den Fall mit gleichzeitigem DB-Ausfall berücksichtigen.

## Unverändert bestehende Backtest-Grenzen

- Der historische Liquiditätsfilter wird weiterhin durch `_UNLIMITED_VOLUME` umgangen.
- Das heutige Kandidatenuniversum wird weiterhin rückwirkend verwendet.

Die zuvor korrigierte Wiedereinstiegssperre ist weiterhin vorhanden. Die beiden übrigen Punkte sind Einschränkungen der historischen Validierung; die Nachprüfung enthält keinen neuen Rentabilitätsnachweis.

## Prüfrahmen

**454 Tests bestanden**, 44 Pandas-Deprecation-Warnungen, Laufzeit 80,71 Sekunden. Das sind acht zusätzliche Tests gegenüber der vorigen Nachprüfung.

Erneut ausgeführt wurde derselbe gezielte Testumfang wie zuvor: Bot-Engine, Guards, Signale, Hyperliquid-Adapter, Live-Vorprüfung, Live/Backtest-Parität, Backtest und Walk-Forward. Ergänzend liefen die oben beschriebenen isolierten Reproduktionen, einschließlich der teilweisen Notfall-Schließung. Die alten Berichte bleiben als Historie erhalten; ihre Aussagen über inzwischen behobene Fehler gelten nicht als aktueller Befund.
