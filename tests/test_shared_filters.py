"""Tests für die gemeinsamen Jinja-Filter (web/shared.py)."""
import re

from web.shared import _fmt_duration, _fmt_ts


def test_fmt_duration_seconds_under_a_minute():
    assert _fmt_duration(45) == "45s"
    assert _fmt_duration(0) == "0s"
    assert _fmt_duration(59) == "59s"


def test_fmt_duration_minutes():
    """Praxisfall: 1265s soll als "21min" erscheinen, nicht sekundengenau --
    das interessiert bei einem mehrminütigen Scan niemanden."""
    assert _fmt_duration(1265) == "21min"
    assert _fmt_duration(60) == "1min"
    assert _fmt_duration(3599) == "59min"


def test_fmt_duration_hours():
    assert _fmt_duration(3600) == "1h"
    assert _fmt_duration(3900) == "1h 5min"
    assert _fmt_duration(7200) == "2h"


def test_fmt_duration_none_and_invalid():
    assert _fmt_duration(None) == "—"
    assert _fmt_duration("garbage") == "—"


def test_fmt_duration_rounds_fractional_seconds():
    assert _fmt_duration(1264.6) == "21min"


def test_fmt_ts_formats_compactly():
    """Praxisfall Systemstatus: "2026-09-14T21:33:43Z" soll kompakt lesbar
    erscheinen ("14.9.26 21:33"), nicht als rohes ISO-Format -- die exakte
    Uhrzeit hängt von der lokalen Zeitzone der Maschine ab, deshalb wird hier nur
    die Form geprüft (Tag.Monat.Jahr Stunde:Minute), nicht ein fixer Wert."""
    result = _fmt_ts("2026-09-14T21:33:43Z")
    assert re.match(r"^\d{1,2}\.\d{1,2}\.\d{2} \d{2}:\d{2}$", result)


def test_fmt_ts_none_and_invalid():
    assert _fmt_ts(None) == "—"
    assert _fmt_ts("garbage") == "garbage"
