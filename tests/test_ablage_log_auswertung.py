import importlib.util
import sys
from pathlib import Path

SKRIPT = Path(__file__).parent.parent / "scripts" / "ablage_log_auswertung.py"


def _modul():
    spec = importlib.util.spec_from_file_location("ablage_log_auswertung", SKRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _log(conn, host, quelle, rang, ms, projekt=1, ts="2026-10-05T10:00:00Z", endung=".pdf"):
    conn.execute("INSERT INTO ablage_log (ts, host, dateiname, endung, gewaehlt_pfad, gewaehlt_rang, projekt_richtig, "
                 "dauer_ms, quelle, seite_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (ts, host, "x" + endung, endung, "/p/x", rang, projekt, 5000, quelle, ms))


def test_auswertung_leer(tmp_db):
    assert "Noch keine Einträge" in _modul().auswerten(tmp_db)


def test_auswertung_wege_zeit_und_rang(tmp_db):
    for ms in (3000, 5000, 7000):
        _log(tmp_db, "mac-1", "option", 1, ms)
    _log(tmp_db, "mac-1", "option", 2, 9000)
    _log(tmp_db, "mac-2", "suche", 0, 12000, projekt=0)
    _log(tmp_db, "mac-2", "browser", 0, 40000)
    _log(tmp_db, "mac-1", "zuletzt", 0, 4000)
    _log(tmp_db, "mac-1", None, 3, None)                       # alter Eintrag ohne Kennzeichen
    tmp_db.commit()
    t = _modul().auswerten(tmp_db)
    assert "Ablagen: **8**" in t and "mac-1, mac-2" in t
    assert "| Vorgeschlagene Option | 50 % | 4 |" in t
    assert "| Ordnersuche | 12 % | 1 | 12.0 s |" in t and "| Ordnerbrowser | 12 % | 1 | 40.0 s |" in t
    assert "| Option 1 | 3 | 38 % |" in t and "| Option 2 | 1 | 12 % |" in t and "| Option 3 | 1 | 12 % |" in t
    assert "| keine Option (Suche, Browser, zuletzt, neuer Ordner) | 3 | 38 % |" in t
    assert "Das Ziel stand unter den Vorschlägen: 62 %" in t        # 5 von 8 mit Rang 1–3
    assert "vorgeschlagene Projekt war richtig: **88 %** (n=8)" in t   # 7 von 8
    assert "ältere Einträge ohne Kennzeichen" in t and "Median Zeit Seitenaufruf → Ablegen" in t


def test_auswertung_filter(tmp_db):
    _log(tmp_db, "mac-1", "option", 1, 1000, ts="2026-09-01T00:00:00Z")
    _log(tmp_db, "mac-2", "suche", 0, 2000, ts="2026-10-10T00:00:00Z")
    tmp_db.commit()
    m = _modul()
    assert "Ablagen: **1**" in m.auswerten(tmp_db, seit="2026-10-01")
    assert "mac-2" in m.auswerten(tmp_db, seit="2026-10-01") and "mac-1" not in m.auswerten(tmp_db, seit="2026-10-01")
    assert "Ablagen: **1**" in m.auswerten(tmp_db, host="mac-1")


def test_auswertung_liest_nur(tmp_db, monkeypatch, capsys):
    from db import connection
    _log(tmp_db, "mac-1", "option", 1, 1000)
    tmp_db.commit()
    tmp_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    monkeypatch.setattr(sys, "argv", ["x", "--db", str(connection.db_path())])
    _modul().main()
    assert "Ablagen: **1**" in capsys.readouterr().out
