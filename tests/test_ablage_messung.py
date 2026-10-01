import hashlib
import importlib.util
import sys
from pathlib import Path

from db import connection

SKRIPT = Path(__file__).parent.parent / "scripts" / "ablage_messung.py"


def _lade_skript():
    spec = importlib.util.spec_from_file_location("ablage_messung", SKRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _db_mit_dateien(conn):
    pid = conn.execute("INSERT INTO projects (name, path) VALUES ('211 Test', '/Volumes/T/211 Test')").lastrowid
    k = 0
    for ordner, namen in {"51_Ausfuehrung/51a_Planstände": ["GR_EG_a.pdf", "GR_EG_b.pdf", "SN_A.pdf"],
                          "b_Vertraege": ["Werkvertrag.pdf", "Werkvertrag_2.pdf"]}.items():
        for n in namen:
            k += 1
            mod = "2026-01-01T00:00:00Z" if k % 2 else "2026-09-01T00:00:00Z"
            did = conn.execute("INSERT INTO documents (project_id, hash, filename, extension, filesize, modified_at)"
                               " VALUES (?,?,?,?,?,?)", (pid, f"h{k}", n, ".pdf", 5, mod)).lastrowid
            conn.execute("INSERT INTO document_paths (document_id, path, is_primary) VALUES (?,?,1)",
                         (did, f"/Volumes/T/211 Test/{ordner}/{n}"))
    conn.commit()


def _hash(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def test_messung_laeuft_nur_lesend(tmp_db, tmp_path, monkeypatch, capsys):
    _db_mit_dateien(tmp_db)
    tmp_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db = str(connection.db_path())
    vorher = _hash(db)
    bericht = tmp_path / "bericht.md"
    skript = _lade_skript()
    monkeypatch.setattr(sys, "argv", ["ablage_messung.py", "--db", db, "--stichtag", "2026-06-01",
                                     "--ausgabe", str(bericht), "--ohne-fts"])
    skript.main()
    text = bericht.read_text(encoding="utf-8")
    for abschnitt in ("## Basiswert", "## Struktur und Statistik-Neuaufbau", "## Merkmals-Abdeckung",
                      "## Stichproben-Dateinamen"):
        assert abschnitt in text
    assert "Stichprobe: 2" in text            # k = 2, 4 liegen nach dem Stichtag
    assert _hash(db) == vorher                # nichts geschrieben


def test_zeit_trennung_basiswert(tmp_db):
    skript = _lade_skript()
    sicht = [{"id": 1, "ordner": "A", "ext": ".pdf", "tokens": {"grundriss"}, "mod": "2026-01-01T00:00:00Z"},
             {"id": 2, "ordner": "B", "ext": ".pdf", "tokens": {"vertrag"}, "mod": "2026-02-01T00:00:00Z"}]
    f = {"filename": "Grundriss neu.pdf", "ext": ".pdf"}
    assert skript.alt_ordner(sicht, f) == "A"                       # Wortüberschneidung
    assert skript.alt_ordner(sicht, {"filename": "x.pdf", "ext": ".pdf"}) == "B"   # Gleichstand → zuletzt
    assert skript.alt_ordner(sicht, {"filename": "x.dwg", "ext": ".dwg"}) is None  # keine Datei dieser Endung
