"""Regressionstest fuer Postfachnamen mit Umlauten/Sonderzeichen (z.B. 'Möbelierung').

Hintergrund: Ein Postfach-Scan schlug mit 'ascii' codec can't encode character '\\xf6'
fehl. _encode_imap_utf7() selbst war korrekt (IMAP modified UTF-7 liefert reinen
ASCII-Output) -- die eigentliche Ursache war die Prozessumgebung: launchd startet
LaunchAgents ohne LANG/LC_ALL, wodurch Python fuer stdout/stderr/Logs teils auf
ASCII statt UTF-8 zurueckfaellt und jede Log-Zeile mit Umlauten crasht (siehe
menubar/server_app.py::_env() und das LaunchAgent-Plist, PYTHONUTF8/PYTHONIOENCODING).
Dieser Test haelt zumindest fest, dass die Encoder-Funktion selbst fuer solche
Namen garantiert ASCII-safe bleibt."""
from scanner.mail_scanner import _encode_imap_utf7


def test_encode_imap_utf7_is_ascii_safe_for_umlauts():
    name = "200_Keller_Winterthur/Möbelierung"
    encoded = _encode_imap_utf7(name)
    encoded.encode("ascii")  # darf nicht raisen
    assert encoded == "200_Keller_Winterthur/M&APY-belierung"


def test_encode_imap_utf7_roundtrip_ascii_names_unchanged():
    name = "219_Feldstrasse_Aarburg"
    assert _encode_imap_utf7(name) == name
