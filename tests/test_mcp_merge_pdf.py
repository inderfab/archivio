"""Tests für /api/mcp/merge-pdf -- führt über MCP gefundene PDF-Dokumente zu einer
Datei zusammen. Anders als /api/pdf-zusammenfuehren (nur vom Web-UI erreichbar) muss
hier jede Id zusätzlich dieselbe Freigabekette wie mcp_document() durchlaufen:
Projekt-Whitelist, Normen-Sperre, Sperrliste -- siehe web/api.py::mcp_merge_pdf()."""
from db import queries
from scanner import block_list
from tests.test_pdf_merge import _client, _make_document, _write_pdf


def _enabled_project(conn, name="P", path="/scan"):
    p = queries.insert_project(conn, name, path)
    conn.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    return p


def test_merges_two_allowed_pdfs(tmp_db, tmp_path):
    from config import settings

    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = _enabled_project(tmp_db)
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _write_pdf(a, pages=1)
    _write_pdf(b, pages=2)
    doc_a = _make_document(tmp_db, p, a, "a.pdf")
    doc_b = _make_document(tmp_db, p, b, "b.pdf")

    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": f"{doc_a},{doc_b}"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["merged"] == 2
    assert data["skipped"] == []

    import base64
    import io

    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(base64.b64decode(data["pdf_base64"])))
    assert len(reader.pages) == 3

    row = tmp_db.execute("SELECT * FROM mcp_log WHERE tool='merge_pdf' ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["status"] == "ok"


def test_document_in_disabled_project_is_skipped_not_merged(tmp_db, tmp_path):
    """Ein zweites, NICHT freigegebenes Projekt darf sich nicht über eine
    gemischte document_ids-Liste einschleichen."""
    from config import settings

    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    allowed = _enabled_project(tmp_db, "Freigegeben", "/scan/frei")
    disabled = queries.insert_project(tmp_db, "Gesperrt", "/scan/gesperrt")
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    _write_pdf(a)
    _write_pdf(b)
    doc_a = _make_document(tmp_db, allowed, a, "a.pdf")
    doc_b = _make_document(tmp_db, disabled, b, "geheim.pdf")

    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": f"{doc_a},{doc_b}"})
    data = r.json()
    assert data["ok"] is True
    assert data["merged"] == 1
    assert len(data["skipped"]) == 1
    assert data["skipped"][0]["filename"] == "geheim.pdf"
    assert "Nicht für Claude freigegeben" in data["skipped"][0]["reason"]


def test_norm_document_is_skipped(tmp_db, tmp_path):
    from config import settings

    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = _enabled_project(tmp_db)
    a, norm = tmp_path / "a.pdf", tmp_path / "sia416.pdf"
    _write_pdf(a)
    _write_pdf(norm)
    doc_a = _make_document(tmp_db, p, a, "a.pdf")
    doc_norm = _make_document(tmp_db, p, norm, "sia416.pdf")
    tmp_db.execute("UPDATE documents SET is_norm=1 WHERE id=?", (doc_norm,))
    tmp_db.commit()

    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": f"{doc_a},{doc_norm}"})
    data = r.json()
    assert data["merged"] == 1
    assert len(data["skipped"]) == 1
    assert data["skipped"][0]["reason"] == "Norm erkannt"


def test_blocked_document_is_skipped_with_reason(tmp_db, tmp_path):
    from config import settings

    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = _enabled_project(tmp_db)
    a, lohn = tmp_path / "a.pdf", tmp_path / "lohn.pdf"
    _write_pdf(a)
    _write_pdf(lohn)
    doc_a = _make_document(tmp_db, p, a, "a.pdf")
    doc_lohn = _make_document(tmp_db, p, lohn, "lohn.pdf")
    lohn_hash = tmp_db.execute("SELECT hash FROM documents WHERE id=?", (doc_lohn,)).fetchone()["hash"]
    tmp_db.execute(
        "INSERT INTO block_rules (type, value, label) VALUES ('file', ?, 'Lohn')", (lohn_hash,)
    )
    tmp_db.commit()
    block_list.reload_block_rules(tmp_db)

    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": f"{doc_a},{doc_lohn}"})
    data = r.json()
    assert data["merged"] == 1
    assert len(data["skipped"]) == 1
    assert "Lohn" in data["skipped"][0]["reason"]


def test_non_pdf_document_is_skipped(tmp_db, tmp_path):
    from config import settings

    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = _enabled_project(tmp_db)
    a = tmp_path / "a.pdf"
    txt = tmp_path / "notiz.txt"
    _write_pdf(a)
    txt.write_text("kein pdf")
    doc_a = _make_document(tmp_db, p, a, "a.pdf")
    doc_txt = _make_document(tmp_db, p, txt, "notiz.txt", extension=".txt")

    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": f"{doc_a},{doc_txt}"})
    data = r.json()
    assert data["merged"] == 1
    assert len(data["skipped"]) == 1
    assert "notiz.txt" in data["skipped"][0]["filename"]


def test_no_allowed_pdfs_returns_400(tmp_db):
    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": "999999"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_empty_document_ids_returns_400(tmp_db):
    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": ""})
    assert r.status_code == 400


def test_invalid_document_ids_returns_400(tmp_db):
    r = _client().get("/api/mcp/merge-pdf", params={"document_ids": "abc,def"})
    assert r.status_code == 400
