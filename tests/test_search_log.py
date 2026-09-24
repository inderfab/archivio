"""Tests für das Suche-Protokoll (scanner/search_log.py) -- jede Suche muss eine
Zeile in search_log hinterlassen, ein Klick auf ein Ergebnis
muss den clicks-Zähler dieser Zeile erhöhen. Bewusst OHNE Bezug zu Person/Gerät."""
from db import queries


def _make_doc(conn, project_id, filename, content):
    doc_id = queries.upsert_document(conn, {
        "project_id":   project_id,
        "hash":         f"h-{filename}",
        "filename":     filename,
        "extension":    ".txt",
        "filesize":     len(content),
        "modified_at":  "2026-01-01T00:00:00Z",
        "source_type":  "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    queries.upsert_content(conn, doc_id, content)
    return doc_id


def test_log_search_inserts_row_and_returns_id(tmp_db):
    from scanner.search_log import log_search

    row_id = log_search(
        tmp_db, "suche", "Grundriss", project_id=None, filters="",
        result_count=3, duration_ms=42,
    )
    assert row_id is not None

    row = tmp_db.execute("SELECT * FROM search_log WHERE id = ?", (row_id,)).fetchone()
    assert row["kind"] == "suche"
    assert row["query"] == "Grundriss"
    assert row["result_count"] == 3
    assert row["duration_ms"] == 42
    assert row["clicks"] == 0


def test_log_click_increments_counter(tmp_db):
    from scanner.search_log import log_click, log_search

    row_id = log_search(tmp_db, "suche", "q", None, "", 1, 10)
    log_click(tmp_db, row_id)
    log_click(tmp_db, row_id)

    row = tmp_db.execute("SELECT clicks FROM search_log WHERE id = ?", (row_id,)).fetchone()
    assert row["clicks"] == 2


def test_cleanup_old_deletes_old_rows_only(tmp_db):
    from scanner.search_log import cleanup_old

    tmp_db.execute(
        "INSERT INTO search_log (ts, kind, query, result_count, duration_ms) "
        "VALUES ('2020-01-01T00:00:00Z', 'suche', 'alt', 0, 1)"
    )
    tmp_db.execute(
        "INSERT INTO search_log (kind, query, result_count, duration_ms) "
        "VALUES ('suche', 'neu', 0, 1)"
    )
    tmp_db.commit()

    deleted = cleanup_old(tmp_db, 30)
    assert deleted == 1
    remaining = [r["query"] for r in tmp_db.execute("SELECT query FROM search_log")]
    assert remaining == ["neu"]


def test_cleanup_old_zero_retention_deletes_nothing(tmp_db):
    from scanner.search_log import cleanup_old

    tmp_db.execute(
        "INSERT INTO search_log (ts, kind, query, result_count, duration_ms) "
        "VALUES ('2020-01-01T00:00:00Z', 'suche', 'alt', 0, 1)"
    )
    tmp_db.commit()

    assert cleanup_old(tmp_db, 0) == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0] == 1


def test_search_route_writes_log_entry_and_returns_search_log_id(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "plan.txt", "Grundriss Erdgeschoss mit Wohnflaeche")
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/search", params={"q": "Grundriss"})
    assert r.status_code == 200

    rows = tmp_db.execute("SELECT * FROM search_log ORDER BY id DESC").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "suche"
    assert rows[0]["query"] == "Grundriss"
    assert rows[0]["result_count"] == 1
    assert rows[0]["duration_ms"] >= 0
    assert f"trackSearchClick({rows[0]['id']})" in r.text


def test_search_log_click_endpoint_increments_clicks(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app
    from scanner.search_log import log_search

    row_id = log_search(tmp_db, "suche", "q", None, "", 1, 10)
    tmp_db.commit()

    c = TestClient(app)
    r = c.post(f"/search-log/{row_id}/click")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    row = tmp_db.execute("SELECT clicks FROM search_log WHERE id = ?", (row_id,)).fetchone()
    assert row["clicks"] == 1


def test_empty_search_does_not_write_log_entry(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.get("/search", params={"q": ""})
    assert r.status_code == 200
    assert tmp_db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0] == 0


def test_log_search_same_token_coalesces_into_one_row(tmp_db):
    """Simuliert einen Tippvorgang ('ne' -> 'net' -> 'netzwerk'): solange kein
    Klick dazwischen liegt, soll nur eine Zeile mit der zuletzt eingetippten
    Anfrage übrig bleiben, nicht drei."""
    from scanner.search_log import log_search

    id1 = log_search(tmp_db, "suche", "ne", None, "", 12, 50, token="tok-1")
    id2 = log_search(tmp_db, "suche", "net", None, "", 8, 60, token="tok-1")
    id3 = log_search(tmp_db, "suche", "netzwerk", None, "", 3, 70, token="tok-1")

    assert id1 == id2 == id3
    assert tmp_db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0] == 1
    row = tmp_db.execute("SELECT * FROM search_log WHERE id = ?", (id3,)).fetchone()
    assert row["query"] == "netzwerk"
    assert row["result_count"] == 3
    assert row["duration_ms"] == 70


def test_log_search_after_click_starts_new_row_for_same_token(tmp_db):
    """Ein Klick beendet das Zusammenfassen -- weiteres Tippen mit demselben
    token (z.B. Verfeinern der Anfrage NACH dem Öffnen eines Treffers) darf die
    bereits abgeschlossene, angeklickte Zeile nicht überschreiben."""
    from scanner.search_log import log_click, log_search

    id1 = log_search(tmp_db, "suche", "netzwerk", None, "", 3, 70, token="tok-1")
    log_click(tmp_db, id1)

    id2 = log_search(tmp_db, "suche", "netzwerk plan", None, "", 1, 40, token="tok-1")

    assert id2 != id1
    assert tmp_db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0] == 2
    row1 = tmp_db.execute("SELECT query, clicks FROM search_log WHERE id = ?", (id1,)).fetchone()
    assert row1["query"] == "netzwerk" and row1["clicks"] == 1


def test_log_search_same_token_outside_window_starts_new_row(tmp_db):
    """Liegt die letzte Zeile mit demselben token schon länger zurück, gilt das
    als eigenständige neue Suche, nicht als Fortsetzung desselben Tippvorgangs."""
    from scanner.search_log import log_search

    tmp_db.execute(
        "INSERT INTO search_log (ts, kind, query, result_count, duration_ms, token) "
        "VALUES ('2020-01-01T00:00:00Z', 'suche', 'alt', 5, 10, 'tok-1')"
    )
    tmp_db.commit()

    new_id = log_search(tmp_db, "suche", "neu", None, "", 2, 20, token="tok-1")
    assert tmp_db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0] == 2
    row = tmp_db.execute("SELECT query FROM search_log WHERE id = ?", (new_id,)).fetchone()
    assert row["query"] == "neu"


def test_log_search_concurrent_same_token_does_not_duplicate(tmp_db):
    """Regression: zwei nahezu gleichzeitige Anfragen mit demselben token (z.B.
    Text tippen und sofort danach einen Filter umschalten, beides innerhalb des
    300ms-Debounce-Fensters) dürfen nicht als zwei Zeilen enden, nur weil beide
    beim SELECT noch keine bestehende Zeile sehen -- siehe BEGIN IMMEDIATE in
    log_search(). Nutzt echte, unabhängige Connections (wie zwei parallele
    HTTP-Requests), nicht dieselbe tmp_db-Connection, sonst würde die Race gar
    nicht erst auftreten können."""
    import threading

    from db import connection
    from scanner.search_log import log_search

    results = []
    barrier = threading.Barrier(2)

    def worker(query):
        conn = connection.get_connection()
        barrier.wait()
        row_id = log_search(conn, "suche", query, None, "", 1, 10, token="race-token")
        results.append(row_id)
        conn.close()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 2
    assert results[0] == results[1]
    count = tmp_db.execute(
        "SELECT COUNT(*) FROM search_log WHERE token = 'race-token'"
    ).fetchone()[0]
    assert count == 1


def test_log_search_without_token_always_inserts(tmp_db):
    from scanner.search_log import log_search

    id1 = log_search(tmp_db, "suche", "a", None, "", 1, 1)
    id2 = log_search(tmp_db, "suche", "b", None, "", 1, 1)
    assert id1 != id2
    assert tmp_db.execute("SELECT COUNT(*) FROM search_log").fetchone()[0] == 2


def test_search_route_with_repeated_token_coalesces_rows(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "plan.txt", "Netzwerkplan Erdgeschoss")
    tmp_db.commit()

    c = TestClient(app)
    c.get("/search", params={"q": "ne", "search_token": "burst-1"})
    r = c.get("/search", params={"q": "netzwerk", "search_token": "burst-1"})
    assert r.status_code == 200

    rows = tmp_db.execute("SELECT * FROM search_log").fetchall()
    assert len(rows) == 1
    assert rows[0]["query"] == "netzwerk"


def test_log_search_stores_query_string(tmp_db):
    from scanner.search_log import log_search

    row_id = log_search(
        tmp_db, "suche", "Vertrag", None, "Typ: pdf", 5, 100,
        query_string="q=Vertrag&type=pdf",
    )
    row = tmp_db.execute("SELECT query_string FROM search_log WHERE id = ?", (row_id,)).fetchone()
    assert row["query_string"] == "q=Vertrag&type=pdf"


def test_log_search_coalesce_updates_query_string(tmp_db):
    """Beim Zusammenfassen eines Tippvorgangs (siehe Token-Tests oben) muss auch
    der query_string auf den zuletzt eingetippten Stand aktualisiert werden --
    sonst würde "Resultate anzeigen" auf eine veraltete Zwischen-Anfrage zeigen."""
    from scanner.search_log import log_search

    row_id = log_search(tmp_db, "suche", "ne", None, "", 12, 50, token="tok-1",
                         query_string="q=ne&search_token=tok-1")
    row_id2 = log_search(tmp_db, "suche", "netzwerk", None, "", 3, 70, token="tok-1",
                          query_string="q=netzwerk&search_token=tok-1")

    assert row_id == row_id2
    row = tmp_db.execute("SELECT query_string FROM search_log WHERE id = ?", (row_id,)).fetchone()
    assert row["query_string"] == "q=netzwerk&search_token=tok-1"


def test_search_route_stores_full_query_string(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_doc(tmp_db, p, "plan.txt", "Grundriss Erdgeschoss")
    tmp_db.commit()

    c = TestClient(app)
    c.get("/search", params={"q": "Grundriss", "project_id": str(p), "type": "pdf"})

    row = tmp_db.execute("SELECT query_string FROM search_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["query_string"] is not None
    assert "q=Grundriss" in row["query_string"]
    assert f"project_id={p}" in row["query_string"]
    assert "type=pdf" in row["query_string"]


def test_system_status_shows_search_result_link_with_query_string(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app
    from scanner.search_log import log_search

    p = queries.insert_project(tmp_db, "Testprojekt", "/scan")
    log_search(tmp_db, "suche", "Vertrag", project_id=p, filters="Typ: pdf",
               result_count=5, duration_ms=120, query_string="q=Vertrag&project_id=%s&type=pdf" % p)
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/system-status")
    assert r.status_code == 200
    assert "Resultate anzeigen" in r.text
    assert 'href="/?q=Vertrag' in r.text
    assert f"project_id={p}" in r.text
    assert "type=pdf" in r.text
    assert "Testprojekt" in r.text
    assert "Typ: pdf" in r.text


def test_search_log_export_csv_contains_entry(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app
    from scanner.search_log import log_click, log_search

    p = queries.insert_project(tmp_db, "P", "/scan")
    row_id = log_search(tmp_db, "suche", "Vertrag", project_id=p, filters="Typ: pdf", result_count=5, duration_ms=120)
    log_click(tmp_db, row_id)
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/search-log/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    assert "Vertrag" in body
    assert "P" in body
    assert "Typ: pdf" in body
    assert "5" in body and "120" in body and "1" in body
