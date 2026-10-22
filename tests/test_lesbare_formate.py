"""Archivio liest jedes Format, fuer das es einen Leser hat -- unabhaengig von config.yaml."""
from db import queries


def test_supported_includes_every_reader(tmp_db):
    from scanner.extractors import _REGISTRY
    from scanner.walker import _supported_extensions

    sup = _supported_extensions()
    assert set(_REGISTRY) <= sup
    assert ".doc" in sup and ".htm" in sup and ".xml" in sup and ".xls" in sup


def test_extract_html_keeps_text_drops_markup(tmp_path):
    from scanner.extractors import extract_html

    f = tmp_path / "a.htm"
    f.write_text("<html><head><style>x{}</style><script>var a=1</script></head>"
                 "<body><h1>Baubeschrieb</h1><p>Fassade &amp; Dach</p></body></html>", encoding="utf-8")
    text, _ = extract_html(f)
    assert "Baubeschrieb" in text and "Fassade & Dach" in text
    assert "var a" not in text and "x{}" not in text


def test_requeue_only_small_readable_listed_files(tmp_db):
    from db.migrations import requeue_newly_readable

    p = queries.insert_project(tmp_db, "P", "/scan")
    rows = [("a.doc", ".doc", 1000, "listed"), ("b.rtf", ".rtf", 1000, "unsupported"),
            ("gross.doc", ".doc", 200 * 1024 * 1024, "listed"), ("c.jpg", ".jpg", 1000, "listed"),
            ("d.pdf", ".pdf", 1000, "listed"), ("e.doc", ".doc", 1000, "ok")]
    for i, (name, ext, size, st) in enumerate(rows):
        tmp_db.execute(
            "INSERT INTO documents (project_id, hash, filename, extension, filesize, extraction_status) "
            "VALUES (?,?,?,?,?,?)", (p, f"h{i}", name, ext, size, st))
    tmp_db.commit()
    requeue_newly_readable(tmp_db)
    st = {r["filename"]: r["extraction_status"] for r in tmp_db.execute("SELECT filename, extraction_status FROM documents")}
    assert st["a.doc"] == "pending" and st["b.rtf"] == "pending"
    assert st["gross.doc"] == "listed" and st["c.jpg"] == "listed" and st["d.pdf"] == "listed"
    assert st["e.doc"] == "ok"
