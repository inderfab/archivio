import textwrap
import pytest
from pathlib import Path
from db import connection
from config import settings


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    # Datenverzeichnis getrennt von den gescannten Dateien — sonst würde der
    # Scanner die DB-/Config-Dateien selbst indexieren (alle Dateien werden erfasst).
    data_dir = tmp_path / "_data"
    data_dir.mkdir()
    cfg = data_dir / "config.yaml"
    cfg.write_text(textwrap.dedent("""\
        database:
          path: archivio.db
        scanner:
          num_workers: 1
          supported_extensions: ['.txt', '.pdf', '.eml', '.xlsx', '.docx', '.rtf']
          excluded_folders: ['Planstande', 'Upload', 'Archiv']
          hash_algorithm: sha256
    """), encoding="utf-8")

    # ARCHIVIO_DATA_DIR wird an spawn-Worker vererbt → alle Prozesse lösen DB +
    # Config identisch auf. Settings-/Connection-Caches im Parent zurücksetzen.
    monkeypatch.setenv("ARCHIVIO_DATA_DIR", str(data_dir))
    monkeypatch.setattr(settings, "_CONFIG_PATH", cfg)
    monkeypatch.setattr(settings, "_settings", {})
    monkeypatch.setattr(connection, "_DB_PATH", None)

    connection.init_schema()
    conn = connection.get_connection()
    yield conn
    conn.close()


@pytest.fixture
def sample_files(tmp_path) -> Path:
    root = tmp_path / "scan"
    root.mkdir()
    (root / "plan.txt").write_text("Grundriss Erdgeschoss\nWohnfläche 120qm", encoding="utf-8")
    (root / "notes.txt").write_text("# Besprechung\nProjekt läuft gut.", encoding="utf-8")
    (root / "binary.xyz").write_bytes(b"\x00\x01\x02")
    # Müll-/Systemdateien — sollen NICHT indexiert werden
    (root / "Thumbs.db").write_bytes(b"junk")
    (root / "~$bericht.docx").write_bytes(b"lock")
    (root / "backup.txt~").write_text("alt", encoding="utf-8")
    (root / "session.lock").write_bytes(b"")
    # Ausgeschlossene Ordner
    for folder in ("Planstande", "Upload", "Archiv"):
        d = root / folder
        d.mkdir()
        (d / "versteckt.txt").write_text("soll nicht indexiert werden", encoding="utf-8")
    return root
