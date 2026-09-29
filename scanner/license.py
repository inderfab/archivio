"""Offline-Lizenzpruefung (HMAC-SHA256) -- siehe PROJEKT_STATUS.md "Lizenzsystem".

Die Pruefung braucht keinerlei Netzwerkzugriff: ein gemeinsames Geheimnis ist fest im
Code eingebaut, der Lizenzschluessel selbst traegt seine Pruefsumme bereits mit sich.

Sicherheitsmodell -- bewusst symmetrisch statt asymmetrisch (Ed25519 wuerde 64-Byte-
Signaturen bedeuten, das ergibt >50 Zeichengruppen; siehe Diskussion in
PROJEKT_STATUS.md): das Geheimnis (LICENSE_SECRET_B64) steckt in der ausgelieferten
Archivio-App. Wer den Python-Code zurueckentwickelt, koennte es extrahieren und damit
selbst gueltige Lizenzen erzeugen. Fuer ein lokales Business-Tool ohne Kopierschutz-
Anspruch (Ziel ist ein Update-Hinweis, keine DRM-Sperre) ist das ein akzeptierter
Trade-off gegen einen kurzen, gut abtippbaren Schluessel.

Lizenzschluessel-Aufbau (vor der Base32-Darstellung), 15 Bytes total:
    1 Byte    Format-Version (aktuell 1)
    2 Bytes   "ausgestellt" als Tage seit EPOCH, big-endian uint16
    2 Bytes   "gueltig_bis" als Tage seit EPOCH, big-endian uint16
    10 Bytes  HMAC-SHA256(secret, obige 5 Bytes)[:10] -- 80 Bit, fuer eine offline
              nicht angreifbare Pruefsumme (kein Orakel, keine Online-Rateversuche
              moeglich) mehr als ausreichend

Praesentationsform: Base32 dieser 15 Bytes (= exakt 24 Zeichen, keine Auffuellung
noetig), in 4er-Gruppen mit "-", Praefix "ARCH-" -- macht 6 Gruppen, z.B.
"ARCH-A8K2-P9XM-4T7Q-B3RS-K7NM-QX2F". Base32 statt Base64: keine Gross-/Klein-
schreibung, keine mit dem Trennstrich verwechselbaren Zeichen.

Der Buero-Name steht bewusst NICHT im Schluessel (spart Platz) -- Fabio haelt ihn in
seinen eigenen Verkaufsunterlagen fest, tools/license_keygen.py fragt ihn nur zur
Anzeige beim Ausstellen ab.

LICENSE_UI_ENABLED ist der einzige Schalter fuer dieses Feature: solange False,
ist das Eingabefeld in den Einstellungen unsichtbar und ein fehlender Schluessel
gilt als "kein Limit" (identisch zum heutigen Verhalten). Der Schalter ist bewusst
keine Einstellung, die Nutzer umschalten koennen, sondern eine Code-Konstante, die
erst mit einer kuenftigen Archivio-Version auf True gesetzt wird.
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# Wird einmalig von Fabio ersetzt, nachdem er
# "tools/license_keygen.py generate-secret" ausgefuehrt hat (siehe dort). Bleibt
# danach über alle künftigen Archivio-Versionen hinweg unveraendert -- sonst
# werden bereits ausgegebene Lizenzschluessel mit einem Update ungueltig.
LICENSE_SECRET_B64 = "REPLACE_WITH_REAL_SECRET_AFTER_KEYGEN"

_EPOCH = date(2025, 1, 1)
_MAC_LEN = 10
GRACE_PERIOD_DAYS = 14

# Der eigentliche Schalter -- siehe Moduldoc oben und PROJEKT_STATUS.md.
LICENSE_UI_ENABLED = False


@dataclass
class LicenseCheck:
    status: str  # "valid" | "grace" | "expired" | "missing" | "invalid"
    ausgestellt: Optional[str] = None
    gueltig_bis: Optional[str] = None
    message: Optional[str] = None


def debug_ui_forced() -> bool:
    """True, wenn die (sonst unsichtbare) Lizenz-UI über die undokumentierte
    Umgebungsvariable ARCHIVIO_DEBUG_LICENSE für Testzwecke sichtbar geschaltet ist.
    Betrifft nur die Sichtbarkeit des Eingabefelds, nicht die Pruefungslogik selbst."""
    return os.environ.get("ARCHIVIO_DEBUG_LICENSE") == "1"


def ui_visible() -> bool:
    return LICENSE_UI_ENABLED or debug_ui_forced()


def _date_to_u16(d: date) -> int:
    return (d - _EPOCH).days


def _u16_to_date(n: int) -> date:
    return _EPOCH + timedelta(days=n)


def _mac(secret: bytes, payload: bytes) -> bytes:
    return hmac.new(secret, payload, hashlib.sha256).digest()[:_MAC_LEN]


def format_license_key(raw_bytes: bytes) -> str:
    b32 = base64.b32encode(raw_bytes).decode("ascii").rstrip("=")
    groups = [b32[i:i + 4] for i in range(0, len(b32), 4)]
    return "ARCH-" + "-".join(groups)


def _unformat(key_str: str) -> bytes:
    cleaned = key_str.strip().upper()
    if cleaned.startswith("ARCH-"):
        cleaned = cleaned[5:]
    cleaned = cleaned.replace("-", "").replace(" ", "")
    cleaned += "=" * ((-len(cleaned)) % 8)
    return base64.b32decode(cleaned)


def build_license_bytes(secret: bytes, ausgestellt: date, gueltig_bis: date) -> bytes:
    payload = bytes([1]) + _date_to_u16(ausgestellt).to_bytes(2, "big") \
        + _date_to_u16(gueltig_bis).to_bytes(2, "big")
    return payload + _mac(secret, payload)


def decode_and_verify(key_str: str) -> Optional[dict]:
    """Gibt {"ausgestellt","gueltig_bis"} (als date) zurueck, wenn Format und
    Pruefsumme gueltig sind, sonst None. Jeder Fehler (falsches Format, manipulierter
    Inhalt, falsche Pruefsumme) fuehrt bewusst zum selben Ergebnis -- der Aufrufer
    muss nicht zwischen "kaputt" und "gefaelscht" unterscheiden."""
    try:
        raw = _unformat(key_str)
        if len(raw) != 5 + _MAC_LEN or raw[0] != 1:
            return None
        payload, received_mac = raw[:5], raw[5:]
        secret = base64.b64decode(LICENSE_SECRET_B64)
        expected_mac = _mac(secret, payload)
        if not hmac.compare_digest(received_mac, expected_mac):
            return None
        ausgestellt = _u16_to_date(int.from_bytes(payload[1:3], "big"))
        gueltig_bis = _u16_to_date(int.from_bytes(payload[3:5], "big"))
        return {"ausgestellt": ausgestellt, "gueltig_bis": gueltig_bis}
    except (ValueError, IndexError, TypeError):
        return None


def check_license() -> LicenseCheck:
    """Liest den in den Einstellungen hinterlegten Lizenzschluessel und prueft ihn.
    Komplett offline, kein Netzwerkzugriff noetig."""
    from config import settings

    key_str = (settings.get("license.key") or "").strip()

    if not key_str:
        if not LICENSE_UI_ENABLED:
            return LicenseCheck(status="missing")
        # Nach Aktivierung: kein Schluessel behandeln wie "innerhalb Kulanzfrist",
        # nicht wie eine harte Sperre -- Bestandsschutz fuer Installationen von
        # vor der Aktivierung (siehe PROJEKT_STATUS.md Abschnitt 5).
        return LicenseCheck(status="grace", message="Kein Lizenzschlüssel hinterlegt.")

    payload = decode_and_verify(key_str)
    if payload is None:
        return LicenseCheck(status="invalid", message="Lizenzschlüssel ungültig.")

    gueltig_bis: date = payload["gueltig_bis"]
    ausgestellt: date = payload["ausgestellt"]
    today = date.today()

    if today <= gueltig_bis:
        return LicenseCheck(status="valid", ausgestellt=ausgestellt.isoformat(),
                             gueltig_bis=gueltig_bis.isoformat())

    grace_until = gueltig_bis + timedelta(days=GRACE_PERIOD_DAYS)
    formatted_date = gueltig_bis.strftime("%d.%m.%Y")
    if today <= grace_until:
        return LicenseCheck(
            status="grace", ausgestellt=ausgestellt.isoformat(), gueltig_bis=gueltig_bis.isoformat(),
            message=f"Lizenz abgelaufen am {formatted_date}, bitte erneuern.",
        )

    # Verhalten ausserhalb der Kulanzfrist ist bewusst noch offen (das Feature ist
    # ohnehin inaktiv) -- siehe PROJEKT_STATUS.md. Status wird trotzdem sauber
    # unterschieden, damit eine kuenftige Entscheidung hier ansetzen kann.
    return LicenseCheck(
        status="expired", ausgestellt=ausgestellt.isoformat(), gueltig_bis=gueltig_bis.isoformat(),
        message=f"Lizenz abgelaufen am {formatted_date}.",
    )


_cached_check: Optional[LicenseCheck] = None


def get_cached_check() -> LicenseCheck:
    global _cached_check
    if _cached_check is None:
        _cached_check = _safe_check()
    return _cached_check


def refresh_check() -> LicenseCheck:
    global _cached_check
    _cached_check = _safe_check()
    return _cached_check


def _safe_check() -> LicenseCheck:
    try:
        return check_license()
    except Exception as exc:
        # Eine fehlerhafte Pruefung darf nie den Serverstart oder den Scheduler
        # gefaehrden -- im Zweifel identisch zu "kein Schluessel hinterlegt".
        log.warning("Lizenzpruefung fehlgeschlagen: %s", exc)
        return LicenseCheck(status="missing")
