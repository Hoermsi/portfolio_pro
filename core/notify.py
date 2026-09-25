"""Push-Benachrichtigungen für den Trading-Bot (aktuell: Telegram).

Bewusst eine einzige generische send()-Funktion statt send_telegram(): der
Aufrufer (bot_runner.py) kennt nur "eine Nachricht rausschicken", nicht
WELCHER Kanal dahinter steckt - ein späterer Wechsel/Zusatz (ntfy.sh,
E-Mail) ändert dann nur diese Datei, keine der vielen Aufrufstellen.

Fire-and-forget: ein Fehlschlag (kein Token, Telegram nicht erreichbar,
Timeout) darf NIE eine Handelsentscheidung beeinflussen oder den
Runner-Prozess zum Absturz bringen - jede Ausnahme wird hier verschluckt.
"""
import requests

from core import config

_TIMEOUT = 10
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send(message: str) -> bool:
    """Best effort - gibt False zurück statt zu werfen, wenn kein Token/
    keine chat_id konfiguriert ist oder der Versand fehlschlägt (Netzwerk,
    Timeout, Telegram-Fehlerantwort)."""
    token, chat_id = config.telegram_credentials()
    if not token or not chat_id:
        return False
    try:
        r = requests.post(_TELEGRAM_API.format(token=token),
                          json={"chat_id": chat_id, "text": message}, timeout=_TIMEOUT)
        return r.ok
    except Exception:
        return False
