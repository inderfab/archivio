"""Online-Abgleich der Aktualität erkannter Normen gegen shop.sia.ch / mobility-
platform.ch (VSS) -- BEWUSST kein Hintergrund-Job (siehe Migration 020), sondern
nur per Button auf /norms ausgelöst. Kontaktiert externe Websites, was ausserhalb
des sonst rein lokalen Prinzips von Archivio liegt -- deshalb nie automatisch/
periodisch, immer nur auf ausdrücklichen Klick.

Best-effort, ohne offizielle API der beiden Shops: findet der Abgleich die
passende Produktseite/den passenden Katalogeintrag nicht eindeutig, bleibt der
Status "ungeprüft" -- es wird NIE geraten, eine Norm sei aktuell oder veraltet,
ohne das auf der jeweiligen Seite tatsächlich gelesen zu haben.

URL-/Textmuster unten wurden gegen echte Seiten verifiziert (2026-09-04):
- shop.sia.ch: Produktseite unter /normenwerk/{kategorie}/{nummer}_{jahr}_d/D/Product,
  aktuelle Ausgabe zeigt "gültig ab", eine abgelöste Ausgabe zusätzlich
  "gültig bis ..., archivierter Titel!".
- mobilityplatform.ch: Katalogsuche unter /de/catalogsearch/result/?q={nummer}
  zeigt pro Treffer "Dokumenten-Nummer SN-{nummer}" und ein Status-Badge
  ("aktiv" / "ausser Kraft") in der Produktkachel davor.
"""
from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger(__name__)

_TIMEOUT = 8
_HEADERS = {"User-Agent": "Mozilla/5.0 (Archivio Norm-Check)"}

# Von check_sia()/check_vss() bei fehlender Internetverbindung zurückgegeben --
# EXAKT dieser Text (nicht nur "ungeprüft"), damit _run_check_all() (web/main.py)
# das erkennen und den ganzen Sammel-Lauf sofort abbrechen kann, statt sich durch
# 300+ Normen zu arbeiten, die alle auf dieselbe Art scheitern würden (Archivio
# läuft bewusst auch komplett offline, siehe Projektprinzip -- dieser Abgleich
# ist der einzige Programmteil, der überhaupt eine Internetverbindung braucht).
OFFLINE_DETAIL = "Kein Internetzugang"


class _OfflineError(Exception):
    """Intern: signalisiert einen Verbindungsfehler (DNS/Verbindung abgelehnt),
    im Unterschied zu einem Timeout nicht transient -- lässt Aufrufer sofort
    abbrechen statt weitere, ebenso aussichtslose Versuche zu unternehmen (z.B.
    alle 5 SIA-Kategorien für dieselbe Norm)."""


def _get_with_retry(url: str, retries: int = 1):
    """Ein Timeout ist bei einem externen Shop nicht selten und rein transient --
    ohne erneuten Versuch sah das im Betrieb wie ein dauerhaft "ungeprüft" aus,
    obwohl ein zweiter Versuch Sekunden später funktioniert hätte. Ein echter 404
    (falsche Kategorie/Jahr) wird NICHT wiederholt, das ist kein Netzwerkfehler.

    Ein ConnectionError (DNS-Fehler, Verbindung abgelehnt/kein Routing -- die
    typische Signatur von "kein Internetzugang") wird NICHT wiederholt und NICHT
    verschluckt, sondern sofort als _OfflineError weitergereicht: anders als ein
    Timeout kommt er praktisch sofort und ein erneuter Versuch kostet nur
    zusätzliche Zeit, ohne je erfolgreich zu sein."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
        except requests.exceptions.ConnectionError as e:
            raise _OfflineError(str(e)) from e
        except Exception as e:
            last_exc = e
            log.debug("Norm-Check: Request an %s fehlgeschlagen (Versuch %d): %s", url, attempt + 1, e)
            if attempt < retries:
                time.sleep(1)
    log.info("Norm-Check: %s nach %d Versuch(en) nicht erreichbar: %s", url, retries + 1, last_exc)
    return None

# Bekannte SIA-Shop-Kategorien -- die Produkt-URL braucht eine davon, welche es
# ist, steht lokal nirgends. Ein paar gängige durchprobieren ist noch "wenig
# Aufwand" (ein paar zusätzliche Requests), eine echte Suche bietet shop.sia.ch
# nicht (die Schnellsuche läuft über JavaScript, kein einfacher GET-Endpunkt).
_SIA_CATEGORIES = ["architekt", "ingenieur", "bauwesen", "haustechnik", "gesamtwerke"]


def _strip_tags(html: str) -> str:
    # <script>/<style>-Inhalt zuerst entfernen -- sonst landen grosse Mengen
    # Boilerplate-JS (Widget-Initialisierung etc.) zwischen den eigentlichen
    # Produktkacheln im Text und verschieben die für den Statusabgleich
    # relevante Nachbarschaft weit ausserhalb jedes sinnvollen Fensters.
    html = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text)


def check_sia(number: str, year: str | None) -> tuple[str, str]:
    """Prüft eine SIA-Norm gegen shop.sia.ch. `number` die reine Normnummer (z.B.
    "400", "4009", "180.081"), `year` das lokal bekannte Ausgabejahr (aus
    norm_valid_from) -- ohne Jahr lässt sich die Produkt-URL nicht zusammensetzen,
    Ergebnis ist dann immer "ungeprüft". Rückgabe: (status, detail-Text)."""
    if not number:
        return "ungeprüft", "Keine erkennbare Normnummer"
    if not year:
        return "ungeprüft", "Kein lokales Ausgabejahr bekannt"
    # Normalisiert auf reine Ziffernfolge -- die URL braucht einen Bindestrich
    # ("118-242_2012_d"), die Produktnummer auf der Seite selbst steht aber mit
    # Schrägstrich ("Produktnummer SIA 118/242"). Ein exakter String-Vergleich
    # verpasste dadurch echte Treffer und lieferte faelschlich "ungeprüft".
    number_key = re.sub(r"[^0-9]", "", number)
    for category in _SIA_CATEGORIES:
        url = f"https://shop.sia.ch/normenwerk/{category}/{number}_{year}_d/D/Product"
        try:
            resp = _get_with_retry(url)
        except _OfflineError:
            # Kein Sinn, die restlichen Kategorien ebenso erfolglos zu versuchen.
            return "ungeprüft", OFFLINE_DETAIL
        if resp is None or resp.status_code != 200:
            continue
        text = _strip_tags(resp.text).lower()
        m_prod = re.search(r"produktnummer sia\s*([0-9/.\-]+)", text)
        if not m_prod or re.sub(r"[^0-9]", "", m_prod.group(1)) != number_key:
            continue  # Seite existiert, aber falsche Kategorie/falsches Produkt erwischt
        if "archivierter titel" in text:
            return "veraltet", f"Auf shop.sia.ch als archivierter Titel markiert ({url})"
        if "gültig ab" in text:
            return "aktuell", f"Auf shop.sia.ch als aktuelle Ausgabe gefunden ({url})"
        return "ungeprüft", f"Seite gefunden, Status nicht eindeutig erkennbar ({url})"
    return "ungeprüft", "Keine passende Produktseite auf shop.sia.ch gefunden"


def check_vss(number: str) -> tuple[str, str]:
    """Prüft eine VSS/SN-Norm gegen die Katalogsuche von mobilityplatform.ch.
    `number` die reine Ziffernfolge (z.B. "640050").

    Jede Produktkachel in den Suchergebnissen hat die Form
    "... Im Webviewer anzeigen Mehr erfahren [ausser Kraft|NEU|<nichts>]
    VSS Dokumenten-Nummer SN[-XX...]-<nummer>[-<suffix>] ..." -- ein Statuswort
    zwischen "Mehr erfahren" und "Dokumenten-Nummer" gibt es NUR bei
    zurückgezogenen ("ausser Kraft") oder brandneuen ("NEU") Ausgaben, eine
    normal aktuelle Ausgabe zeigt dort gar kein Badge. Deshalb: kein Treffer für
    "ausser Kraft" im Fenster davor + die Nummer wurde gefunden => aktuell."""
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return "ungeprüft", "Keine erkennbare Normnummer"
    url = f"https://www.mobilityplatform.ch/de/catalogsearch/result/?q={digits}"
    try:
        resp = _get_with_retry(url)
    except _OfflineError:
        return "ungeprüft", OFFLINE_DETAIL
    if resp is None:
        return "ungeprüft", "Shop nicht erreichbar"
    if resp.status_code != 200:
        return "ungeprüft", f"Shop-Fehler ({resp.status_code})"
    text = _strip_tags(resp.text)
    # Erster (relevantester, laut Standard-Sortierung der Suche) Treffer mit
    # dieser Nummer als eigenständigem Zahlenblock -- egal ob als "SN-<nummer>"
    # oder als Teil eines zusammengesetzten EN/ISO-Codes wie "SN-EN-ISO-13473-1".
    m = re.search(r"Dokumenten-Nummer\s+\S*?" + re.escape(digits) + r"(?!\d)", text, re.IGNORECASE)
    if not m:
        return "ungeprüft", "Norm nicht eindeutig in der Katalogsuche gefunden"
    window = text[max(0, m.start() - 200):m.start()]
    if re.search(r"\bausser kraft\b", window, re.IGNORECASE):
        return "veraltet", f"Laut mobilityplatform.ch «ausser Kraft» ({url})"
    return "aktuell", f"Auf mobilityplatform.ch gefunden, kein Rückzugs-Vermerk ({url})"


def check_norm(number: str | None, year: str | None, source: str) -> tuple[str, str]:
    """Dispatcht auf check_sia()/check_vss() je nach erkanntem Herausgeber (siehe
    scanner.norms.guess_norm_type()). Für alle anderen Herausgeber (EN/DIN/ISO/IEC,
    ohne Schweizer Vertriebsshop) gibt es keinen automatischen Abgleich.

    `year` kommt üblicherweise aus documents.norm_valid_from -- einem vollen
    ISO-Datum ("2020-01-01"), nicht nur einer Jahreszahl. Hier auf die ersten 4
    Ziffern kürzen, das erwartet check_sia() für die Shop-URL."""
    src = (source or "").upper()
    if not number:
        return "ungeprüft", "Keine erkennbare Normnummer"
    if src in ("VSS", "SN"):
        return check_vss(number)
    if src == "SIA":
        return check_sia(number, (year or "")[:4] or None)
    return "ungeprüft", f"Kein automatischer Abgleich für Herausgeber «{source}» verfügbar"
