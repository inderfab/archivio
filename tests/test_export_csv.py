"""Verlaufs-Exporte: Klartext vs. anonymisiert (web/export_csv.py)."""
import csv
import io
import json
import zipfile

from web import export_csv as ex


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def _seed(conn):
    with conn:
        conn.execute("INSERT INTO projects (id, name, path) VALUES (7, 'Wiesenweg Seldwyla', '/nas/219 Wiesenweg')")
        conn.execute(
            "INSERT INTO search_log (ts, kind, query, project_id, filters, result_count, duration_ms, clicks) "
            "VALUES ('2026-10-20T06:00:00Z','suche','Brandschutz Gipser Muster',7,'Typ: .pdf',61,321,1)")
        conn.execute(
            "INSERT INTO mcp_log (ts, tool, query, project_id, files_json, chars_sent, blocked_json, status) "
            "VALUES ('2026-10-20T07:00:00Z','search','u wert glas',7,?,500,?, 'ok')",
            (json.dumps([{"filename": "Glasfreigabe Kunde X.pdf", "path": "/nas/a/Glasfreigabe Kunde X.pdf"}]),
             json.dumps([{"path": "/nas/n/SIA 400.pdf", "filename": "SIA 400.pdf", "reason": "Sperrliste: Ordner Geheim"}])))
        conn.execute(
            "INSERT INTO scan_log (project_id, project_name, started_at, finished_at, duration_s, status, total, "
            "processed, new_count, skipped, error_count, errors_json, peak_memory_mb, peak_cpu_pct) "
            "VALUES (7,'Wiesenweg Seldwyla','2026-10-20T01:00:00Z','2026-10-20T01:10:00Z',600,'done',10,10,2,8,1,?,900,55)",
            (json.dumps([{"filename": "Vertrag Meier.pdf", "path": "/nas/219/Vertrag Meier.pdf"}]),))


def test_norm_mode_defaults_to_anonym():
    assert ex.norm_mode(None) == ex.ANONYM
    assert ex.norm_mode("quatsch") == ex.ANONYM
    assert ex.norm_mode("klartext") == ex.KLARTEXT


def test_search_klartext_and_anonym(tmp_db):
    _seed(tmp_db)
    kl = ex.search_csv(tmp_db, ex.KLARTEXT)
    assert "Brandschutz Gipser Muster" in kl and "Wiesenweg Seldwyla" in kl
    an = ex.search_csv(tmp_db, ex.ANONYM)
    assert "Brandschutz" not in an and "Feldstrasse" not in an
    r = _rows(an)
    assert r[0][2:4] == ["Wortanzahl", "Länge (Zeichen)"]
    assert r[1][2:5] == ["3", str(len("Brandschutz Gipser Muster")), "Projekt 7"]
    assert r[1][5:9] == ["Typ: .pdf", "61", "321", "1"]


def test_mcp_anonym_has_no_names_or_paths_or_rule_labels(tmp_db):
    _seed(tmp_db)
    an = ex.mcp_csv(tmp_db, ex.ANONYM)
    for geheim in ("Glasfreigabe", "Kunde X", "SIA 400", "Geheim", "u wert", "/nas", "Feldstrasse"):
        assert geheim not in an
    row = _rows(an)[1]
    assert ".pdf" in row and "Sperrliste" in row and "Projekt 7" in row
    kl = ex.mcp_csv(tmp_db, ex.KLARTEXT)
    assert "Glasfreigabe Kunde X.pdf" in kl and "u wert glas" in kl


def test_scan_anonym_hides_project_and_paths(tmp_db):
    _seed(tmp_db)
    an = ex.scan_csv(tmp_db, ex.ANONYM)
    assert "Feldstrasse" not in an and "Meier" not in an and "/nas" not in an
    row = _rows(an)[1]
    assert "Projekt 7" in row and ".pdf" in row and "600" in row[2]
    kl = ex.scan_csv(tmp_db, ex.KLARTEXT)
    assert "Wiesenweg Seldwyla" in kl and "/nas/219/Vertrag Meier.pdf" in kl


def test_docs_csv_runs_and_anonymises(tmp_db):
    with tmp_db:
        tmp_db.execute("INSERT INTO projects (id, name, path) VALUES (1,'P','/nas/p')")
        tmp_db.execute(
            "INSERT INTO documents (project_id, hash, filename, extension, filesize, extraction_status) "
            "VALUES (1,'h1','Geheimer Plan.pdf','.pdf',100,'error')")
        did = tmp_db.execute("SELECT id FROM documents").fetchone()[0]
        tmp_db.execute("INSERT INTO document_paths (document_id, path, is_primary) VALUES (?, '/nas/p/Geheimer Plan.pdf', 1)", (did,))
    kl = ex.docs_csv(tmp_db, ex.KLARTEXT)
    assert "Geheimer Plan.pdf" in kl
    an = ex.docs_csv(tmp_db, ex.ANONYM)
    assert "Geheimer" not in an and "/nas" not in an and ".pdf" in an


def test_zip_contains_all_four_and_readme(tmp_db):
    _seed(tmp_db)
    z = zipfile.ZipFile(io.BytesIO(ex.build_zip(tmp_db, ex.ANONYM)))
    assert sorted(z.namelist()) == ["LIESMICH.txt", "dokumentenverarbeitung.csv", "mcp.csv", "scan.csv", "suche.csv"]
    assert "Anonymisiert" in z.read("LIESMICH.txt").decode()


def test_scan_klartext_caps_error_file_list(tmp_db):
    errs = [{"filename": f"f{i}.pdf", "path": f"/nas/f{i}.pdf"} for i in range(50)]
    with tmp_db:
        tmp_db.execute(
            "INSERT INTO scan_log (project_id, project_name, started_at, status, total, processed, new_count, "
            "skipped, error_count, errors_json) VALUES (1,'P','2026-10-20T01:00:00Z','done',50,50,0,0,50,?)",
            (json.dumps(errs),))
    row = _rows(ex.scan_csv(tmp_db, ex.KLARTEXT))[1]
    assert row[-1].count("/nas/f") == ex.MAX_FEHLERDATEIEN
    assert row[-1].endswith("… und 30 weitere")
