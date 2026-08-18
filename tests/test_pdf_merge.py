"""Tests für /api/pdf-zusammenfuehren -- mehrere ausgewählte PDF-Dokumente zu einem
neuen PDF zusammenführen (Suchergebnisse/Foto-Galerie-Auswahl)."""
import base64
import io

from config import settings
from db import queries


def _make_document(conn, project_id, path, filename, extension=".pdf"):
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{path}",
        "filename":    filename,
        "extension":   extension,
        "filesize":    path.stat().st_size if path.exists() else 0,
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, str(path), True)
    conn.commit()
    return doc_id


def _write_pdf(path, pages=1):
    import pypdf
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as f:
        writer.write(f)


def _client():
    from fastapi.testclient import TestClient
    from web.main import app
    return TestClient(app)


def test_merge_two_pdfs_succeeds(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    _write_pdf(a, pages=1)
    _write_pdf(b, pages=2)
    doc_a = _make_document(tmp_db, p, a, "a.pdf")
    doc_b = _make_document(tmp_db, p, b, "b.pdf")

    c = _client()
    r = c.post("/api/pdf-zusammenfuehren", json={"ids": [doc_a, doc_b]})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["merged"] == 2
    assert data["skipped"] == []

    import pypdf
    pdf_bytes = base64.b64decode(data["pdf_base64"])
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 3  # 1 + 2 Seiten


def test_merge_skips_non_pdf(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    a = tmp_path / "a.pdf"
    txt = tmp_path / "notiz.txt"
    _write_pdf(a)
    txt.write_text("kein pdf")
    doc_a = _make_document(tmp_db, p, a, "a.pdf")
    doc_txt = _make_document(tmp_db, p, txt, "notiz.txt", extension=".txt")

    c = _client()
    r = c.post("/api/pdf-zusammenfuehren", json={"ids": [doc_a, doc_txt]})
    data = r.json()
    assert data["ok"] is True
    assert data["merged"] == 1
    assert len(data["skipped"]) == 1
    assert "notiz.txt" in data["skipped"][0]


def test_merge_all_non_pdf_returns_400(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    txt = tmp_path / "notiz.txt"
    txt.write_text("x")
    doc_txt = _make_document(tmp_db, p, txt, "notiz.txt", extension=".txt")

    c = _client()
    r = c.post("/api/pdf-zusammenfuehren", json={"ids": [doc_txt]})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_merge_empty_selection_400(tmp_db):
    c = _client()
    r = c.post("/api/pdf-zusammenfuehren", json={"ids": []})
    assert r.status_code == 400


def test_merge_rejects_path_outside_base_folders(tmp_db, tmp_path, monkeypatch):
    """Sicherheitsrelevant: Dokument dessen Pfad ausserhalb der konfigurierten
    NAS-Wurzelpfade liegt, wird übersprungen -- niemals gelesen."""
    outside = tmp_path / "ausserhalb"
    outside.mkdir()
    a = outside / "geheim.pdf"
    _write_pdf(a)
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path / "erlaubt")}]
    p = queries.insert_project(tmp_db, "P", str(outside))
    doc_a = _make_document(tmp_db, p, a, "geheim.pdf")

    c = _client()
    r = c.post("/api/pdf-zusammenfuehren", json={"ids": [doc_a]})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_merge_unknown_document_id_skipped(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    a = tmp_path / "a.pdf"
    _write_pdf(a)
    doc_a = _make_document(tmp_db, p, a, "a.pdf")

    c = _client()
    r = c.post("/api/pdf-zusammenfuehren", json={"ids": [doc_a, 999999]})
    data = r.json()
    assert data["ok"] is True
    assert data["merged"] == 1
    assert len(data["skipped"]) == 1
