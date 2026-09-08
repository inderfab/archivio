"""Tests für die /norms-Routen des manuellen Aktualitäts-Abgleichs (siehe
web/main.py::norms_check_one/_run_check_all, scanner/norm_freshness.py). Der
eigentliche Online-Abgleich wird überall gemockt -- diese Tests dürfen nicht
vom echten Internet abhängen."""
from db import queries


def _make_norm_doc(conn, project_id, filename, valid_from=None):
    doc_id = queries.upsert_document(conn, {
        "project_id":   project_id,
        "hash":         f"h-{filename}",
        "filename":     filename,
        "extension":    ".pdf",
        "filesize":     1,
        "modified_at":  "2026-01-01T00:00:00Z",
        "source_type":  "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "ok")
    queries.upsert_path(conn, doc_id, f"/scan/{filename}", True)
    queries.upsert_content(conn, doc_id, f"Norm {filename}")
    conn.execute(
        "UPDATE documents SET is_norm=1, norm_valid_from=? WHERE id=?",
        (valid_from, doc_id),
    )
    conn.commit()
    return doc_id


def test_norms_list_includes_validity_and_status(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_norm_doc(tmp_db, p, "400.pdf", valid_from="2018-04-01")

    c = TestClient(app)
    r = c.get("/norms/list")
    assert r.status_code == 200
    assert "2018-04-01" in r.text
    assert "Ungeprüft" in r.text  # Default-Status vor jeder Prüfung


def test_norms_check_one_updates_status(tmp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_norm_doc(tmp_db, p, "400.pdf", valid_from="2018-04-01")

    monkeypatch.setattr("scanner.norm_freshness.check_norm", lambda number, year, source: ("aktuell", "x"))

    c = TestClient(app)
    r = c.post(f"/norms/check/{doc_id}")
    assert r.status_code == 200
    assert "Aktuell" in r.text

    row = tmp_db.execute(
        "SELECT norm_check_status, norm_checked_at FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["norm_check_status"] == "aktuell"
    assert row["norm_checked_at"] is not None


def test_norms_check_one_backfills_missing_valid_from_before_checking(tmp_db, monkeypatch):
    """Praxisfall: eine Norm wurde schon vor Migration 020 erkannt (oder der
    separate "Alle Dokumente nochmals prüfen"-Lauf wurde nie ausgeführt) --
    norm_valid_from ist NULL. check_sia() bräuchte aber das Jahr und bräche sonst
    sofort mit "ungeprüft" ab, ohne überhaupt einen Request zu versuchen. Der
    Online-Abgleich muss das Datum bei Bedarf selbst nachtragen."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h1", "filename": "180.081.pdf",
        "extension": ".pdf", "filesize": 1, "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(tmp_db, doc_id, "ok")
    queries.upsert_path(tmp_db, doc_id, "/scan/180.081.pdf", True)
    queries.upsert_content(tmp_db, doc_id, "SIA 180.081 Bauwesen\nGültig ab: 2018-04-01\n...")
    tmp_db.execute("UPDATE documents SET is_norm=1, norm_valid_from=NULL WHERE id=?", (doc_id,))
    tmp_db.commit()

    calls = []
    monkeypatch.setattr(
        "scanner.norm_freshness.check_norm",
        lambda number, year, source: calls.append((number, year, source)) or ("aktuell", "x"),
    )

    c = TestClient(app)
    r = c.post(f"/norms/check/{doc_id}")
    assert r.status_code == 200
    assert "2018-04-01" in r.text  # Gültigkeit-Spalte jetzt befüllt
    assert "Aktuell" in r.text

    assert calls == [("180.081", "2018-04-01", "SIA")], "check_norm() muss das nachgetragene Datum bekommen"

    row = tmp_db.execute("SELECT norm_valid_from FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["norm_valid_from"] == "2018-04-01"


def test_norms_check_one_reextracts_from_disk_when_cached_text_has_no_date(tmp_db, monkeypatch, tmp_path):
    """Praxisfall SIA 142: der gespeicherte Volltext ist veraltet (z.B. Datei seit
    dem letzten Scan durch eine neue Ausgabe ersetzt) oder von schwacher OCR-
    Qualität und enthält kein Gültigkeitsdatum. Statt sofort aufzugeben, muss der
    Button einmal frisch von der echten Datei auf der Platte neu extrahieren --
    komplett lokal, kein Internet -- und danach mit dem frischen Text
    weiterarbeiten (auch für Typ/Nummer, nicht nur das Datum)."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    real_file = tmp_path / "142.pdf"
    real_file.write_bytes(b"dummy-pdf-bytes")
    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h1", "filename": "142.pdf",
        "extension": ".pdf", "filesize": 1, "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(tmp_db, doc_id, "ok")
    queries.upsert_path(tmp_db, doc_id, str(real_file), True)
    queries.upsert_content(tmp_db, doc_id, "Alter, unbrauchbarer OCR-Text ohne erkennbares Datum")
    tmp_db.execute("UPDATE documents SET is_norm=1, norm_valid_from=NULL WHERE id=?", (doc_id,))
    tmp_db.commit()

    def fake_extract_and_store(conn, doc_id_, path):
        assert path == real_file, "muss die echte Datei vom Datenträger neu einlesen"
        queries.upsert_content(conn, doc_id_, "SIA 142:2025 Bauwesen\nGültig ab: 2025-08-01")
        conn.execute("UPDATE documents SET norm_valid_from=? WHERE id=?", ("2025-08-01", doc_id_))
        conn.commit()

    monkeypatch.setattr("scanner.walker._extract_and_store", fake_extract_and_store)
    calls = []
    monkeypatch.setattr(
        "scanner.norm_freshness.check_norm",
        lambda number, year, source: calls.append((number, year, source)) or ("aktuell", "x"),
    )

    c = TestClient(app)
    r = c.post(f"/norms/check/{doc_id}")
    assert r.status_code == 200
    assert "2025-08-01" in r.text
    assert "Aktuell" in r.text
    assert calls == [("142", "2025-08-01", "SIA")], "frisch extrahierter Typ/Nummer müssen verwendet werden"

    row = tmp_db.execute("SELECT norm_valid_from FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["norm_valid_from"] == "2025-08-01"


def test_run_check_all_does_not_reextract_from_disk(tmp_db, monkeypatch):
    """Der Sammel-Lauf über alle Normen darf NICHT frisch neu extrahieren -- eine
    OCR-Neuextraktion kann pro Datei mehrere Sekunden dauern, bei 300+ Normen
    würde das den ohnehin ca. 30-minütigen Lauf massiv verlangsamen. Das ist dem
    einzelnen manuellen Button-Klick vorbehalten."""
    import web.main as main

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_norm_doc(tmp_db, p, "142.pdf", valid_from=None)

    calls = []
    monkeypatch.setattr("scanner.walker._extract_and_store", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(
        "scanner.norm_freshness.check_norm",
        lambda number, year, source: ("ungeprüft", "x"),
    )
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main._norm_check_all.update({"status": "running", "done": 0, "total": 0})
    main._run_check_all()

    assert calls == [], "Sammel-Lauf darf keine Neuextraktion auslösen"


def test_norms_check_one_404_for_unknown_document(tmp_db):
    from fastapi.testclient import TestClient
    from web.main import app

    c = TestClient(app)
    r = c.post("/norms/check/999999")
    assert r.status_code == 404


def test_run_check_all_updates_every_norm(tmp_db, monkeypatch):
    """Direkter Aufruf von _run_check_all() statt über den Thread-startenden
    Endpunkt -- deterministisch statt von Thread-Timing abhängig (gleiches
    Muster wie test_full_scan_writes_scan_log_via_run_scan)."""
    import web.main as main

    p = queries.insert_project(tmp_db, "P", "/scan")
    d1 = _make_norm_doc(tmp_db, p, "400.pdf", valid_from="2018-04-01")
    d2 = _make_norm_doc(tmp_db, p, "SN_640050.pdf", valid_from=None)

    calls = []

    def fake_check_norm(number, year, source):
        calls.append((number, year, source))
        return ("veraltet", "x")

    monkeypatch.setattr("scanner.norm_freshness.check_norm", fake_check_norm)
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)  # Höflichkeitspause überspringen

    main._norm_check_all.update({"status": "running", "done": 0, "total": 0})
    main._run_check_all()

    assert main._norm_check_all["status"] == "done"
    assert main._norm_check_all["done"] == 2
    assert main._norm_check_all["total"] == 2
    assert len(calls) == 2

    for doc_id in (d1, d2):
        row = tmp_db.execute(
            "SELECT norm_check_status FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        assert row["norm_check_status"] == "veraltet"


def test_run_check_all_stops_when_cancelled(tmp_db, monkeypatch):
    import web.main as main

    p = queries.insert_project(tmp_db, "P", "/scan")
    _make_norm_doc(tmp_db, p, "400.pdf")
    _make_norm_doc(tmp_db, p, "401.pdf")

    calls = []

    def fake_check_norm(number, year, source):
        calls.append(number)
        main._norm_check_all["status"] = "cancelled"  # Abbruch nach dem ersten Aufruf
        return ("aktuell", "x")

    monkeypatch.setattr("scanner.norm_freshness.check_norm", fake_check_norm)
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main._norm_check_all.update({"status": "running", "done": 0, "total": 0})
    main._run_check_all()

    assert len(calls) == 1, "Abbruch soll nach der laufenden Prüfung sofort greifen"


def test_run_check_all_aborts_after_two_consecutive_offline_results(tmp_db, monkeypatch):
    """Praxisfall: Archivio läuft komplett offline (bewusst möglich, siehe
    Projektprinzip). Ohne Internetzugang darf der Sammel-Lauf nicht alle 300+
    Normen durchprobieren -- jede würde ohnehin identisch scheitern. Nach 2
    aufeinanderfolgenden Offline-Ausfällen (nicht schon beim ersten, könnte auch
    nur diese eine Norm betreffen) bricht der ganze Lauf ab."""
    import web.main as main
    from scanner.norm_freshness import OFFLINE_DETAIL

    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(5):
        _make_norm_doc(tmp_db, p, f"{400 + i}.pdf", valid_from="2020-01-01")

    calls = []
    monkeypatch.setattr(
        "scanner.norm_freshness.check_norm",
        lambda number, year, source: calls.append(number) or ("ungeprüft", OFFLINE_DETAIL),
    )
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main._norm_check_all.update({"status": "running", "done": 0, "total": 0})
    main._run_check_all()

    assert len(calls) == 2, "muss nach 2 Folge-Ausfällen abbrechen, nicht alle 5 versuchen"
    assert main._norm_check_all["status"] == "offline"
    assert main._norm_check_all["done"] == 2


def test_run_check_all_does_not_abort_on_isolated_offline_result(tmp_db, monkeypatch):
    """Ein einzelner Offline-Ausfall zwischen zwei erfolgreichen Prüfungen darf
    den Lauf nicht abbrechen -- könnte auch nur diese eine Norm/Anfrage betreffen
    (z.B. ein kurzzeitig überlasteter Shop), nicht zwingend fehlendes Internet."""
    import web.main as main
    from scanner.norm_freshness import OFFLINE_DETAIL

    p = queries.insert_project(tmp_db, "P", "/scan")
    for i in range(3):
        _make_norm_doc(tmp_db, p, f"{400 + i}.pdf", valid_from="2020-01-01")

    results = [("aktuell", "x"), ("ungeprüft", OFFLINE_DETAIL), ("aktuell", "x")]
    monkeypatch.setattr(
        "scanner.norm_freshness.check_norm",
        lambda number, year, source: results.pop(0),
    )
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main._norm_check_all.update({"status": "running", "done": 0, "total": 0})
    main._run_check_all()

    assert main._norm_check_all["status"] == "done"
    assert main._norm_check_all["done"] == 3


def test_check_all_progress_shows_offline_message(tmp_db):
    from fastapi.testclient import TestClient
    import web.main as main
    from web.main import app

    c = TestClient(app)
    main._norm_check_all.update({"status": "offline", "done": 2, "total": 5, "list_refreshed": False})
    r = c.get("/norms/check-all/progress")
    assert "Kein Internetzugang" in r.text
    assert "2/5" in r.text


def test_norms_page_load_clears_stale_offline_banner(tmp_db):
    from fastapi.testclient import TestClient
    import web.main as main
    from web.main import app

    c = TestClient(app)
    main._norm_check_all.update({"status": "offline", "done": 2, "total": 5, "list_refreshed": False})
    c.get("/norms")
    assert main._norm_check_all["status"] == "idle"


def test_run_check_all_resumes_without_rechecking_recently_confirmed_norms(tmp_db, monkeypatch):
    """Praxisfall: ein Komplettlauf über alle Normen dauert ca. 30 Minuten -- bei
    einem Abbruch/Absturz mittendrin darf ein erneuter Lauf NICHT wieder bei Norm 1
    beginnen, sonst wäre die bereits investierte Zeit verloren. Bereits mit
    'aktuell'/'veraltet' bestätigte Normen müssen übersprungen werden (solange die
    Prüfung nicht schon _RECHECK_AFTER_DAYS zurückliegt, siehe nächster Test),
    nur noch 'ungeprüft' gebliebene werden erneut versucht."""
    import web.main as main
    from datetime import datetime, timezone

    p = queries.insert_project(tmp_db, "P", "/scan")
    already_confirmed = _make_norm_doc(tmp_db, p, "400.pdf", valid_from="2020-01-01")
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_db.execute(
        "UPDATE documents SET norm_check_status='aktuell', norm_checked_at=? WHERE id=?",
        (recent, already_confirmed),
    )
    still_open = _make_norm_doc(tmp_db, p, "401.pdf", valid_from="2020-01-01")
    tmp_db.commit()

    calls = []
    monkeypatch.setattr(
        "scanner.norm_freshness.check_norm",
        lambda number, year, source: calls.append(number) or ("aktuell", "x"),
    )
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main._norm_check_all.update({"status": "running", "done": 0, "total": 0})
    main._run_check_all()

    assert calls == ["401"], "frisch bestätigte Norm (400) darf nicht erneut geprüft werden"
    assert main._norm_check_all["total"] == 1


def test_run_check_all_rechecks_stale_confirmed_norms(tmp_db, monkeypatch):
    """Ohne erneute Prüfung nach einer Weile bliebe eine inzwischen abgelöste Norm
    für immer als "Aktuell" stehen, nur weil sie einmal bestätigt wurde -- nach
    _RECHECK_AFTER_DAYS muss sie wieder einbezogen werden."""
    import web.main as main
    from datetime import datetime, timedelta, timezone

    p = queries.insert_project(tmp_db, "P", "/scan")
    stale = _make_norm_doc(tmp_db, p, "400.pdf", valid_from="2020-01-01")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=main._RECHECK_AFTER_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp_db.execute(
        "UPDATE documents SET norm_check_status='aktuell', norm_checked_at=? WHERE id=?",
        (old_ts, stale),
    )
    tmp_db.commit()

    calls = []
    monkeypatch.setattr(
        "scanner.norm_freshness.check_norm",
        lambda number, year, source: calls.append(number) or ("veraltet", "x"),
    )
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)

    main._norm_check_all.update({"status": "running", "done": 0, "total": 0})
    main._run_check_all()

    assert calls == ["400"], "über 30 Tage alte Bestätigung muss erneut geprüft werden"
    row = tmp_db.execute("SELECT norm_check_status FROM documents WHERE id=?", (stale,)).fetchone()
    assert row["norm_check_status"] == "veraltet"


def test_check_all_progress_auto_refreshes_list_only_once(tmp_db):
    """Nach Abschluss soll die Normenliste sich EINMAL automatisch aktualisieren
    (der versteckte hx-trigger="load"-Block), nicht bei jedem weiteren Poll (alle
    2s), solange der "Geprüft"-Banner noch sichtbar ist."""
    from fastapi.testclient import TestClient
    import web.main as main
    from web.main import app

    c = TestClient(app)
    main._norm_check_all.update({"status": "done", "done": 5, "total": 5, "list_refreshed": False})

    r1 = c.get("/norms/check-all/progress")
    assert 'hx-trigger="load"' in r1.text

    r2 = c.get("/norms/check-all/progress")
    assert 'hx-trigger="load"' not in r2.text


def test_check_all_progress_shows_running_and_done(tmp_db):
    from fastapi.testclient import TestClient
    import web.main as main
    from web.main import app

    c = TestClient(app)

    main._norm_check_all.update({"status": "running", "done": 1, "total": 3})
    r = c.get("/norms/check-all/progress")
    assert "1/3" in r.text
    assert "Abbrechen" in r.text

    main._norm_check_all.update({"status": "done", "done": 3, "total": 3})
    r = c.get("/norms/check-all/progress")
    assert "3/3" in r.text
    assert "Liste aktualisieren" in r.text

    main._norm_check_all.update({"status": "idle", "done": 0, "total": 0})
    r = c.get("/norms/check-all/progress")
    assert r.text.strip() == ""


def test_norms_page_load_clears_stale_done_banner(tmp_db):
    """Praxisfall: '✓ Geprüft: 440/440' blieb stehen, auch nachdem man die Seite
    verlassen und neu geöffnet hatte -- ein frischer Aufruf von /norms muss eine
    ABGESCHLOSSENE Meldung wegräumen (nicht aber eine noch laufende Prüfung)."""
    from fastapi.testclient import TestClient
    import web.main as main
    from web.main import app

    c = TestClient(app)

    main._norm_check_all.update({"status": "done", "done": 440, "total": 440})
    c.get("/norms")
    assert main._norm_check_all["status"] == "idle"

    main._norm_check_all.update({"status": "running", "done": 2, "total": 5})
    c.get("/norms")
    assert main._norm_check_all["status"] == "running", "eine laufende Prüfung darf nicht abgebrochen wirken"


def test_norms_unmark_clears_flag_and_sets_manual_override(tmp_db):
    """Praxisfall: Korrespondenz/Berechnungen, die eine Norm nur ERWÄHNEN, werden
    fälschlich als Norm klassifiziert (Herausgeber-Vermerk + Normnummer im
    Text reichen für die Punkteschwelle). Der Nutzer muss das gezielt für ein
    einzelnes Dokument korrigieren können, ohne die Klassifikation insgesamt
    zu lockern."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_norm_doc(tmp_db, p, "SIA_TG_Einladung.pdf")

    c = TestClient(app)
    r = c.post(f"/norms/unmark/{doc_id}")
    assert r.status_code == 200
    assert r.text == ""  # Zeile verschwindet aus der Liste

    row = tmp_db.execute(
        "SELECT is_norm, norm_manual FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    assert row["is_norm"] == 0
    assert row["norm_manual"] == 1


def test_norms_unmark_survives_reclassify(tmp_db):
    """norm_manual=1 muss einen künftigen Reclassify-Lauf davon abhalten, das
    Dokument erneut als Norm zu markieren -- sonst wäre die Korrektur beim
    nächsten Klick auf "Alle Dokumente nochmals prüfen" wieder verloren."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    doc_id = _make_norm_doc(tmp_db, p, "SIA_TG_Einladung.pdf")

    c = TestClient(app)
    c.post(f"/norms/unmark/{doc_id}")
    c.post("/norms/reclassify")

    row = tmp_db.execute("SELECT is_norm FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert row["is_norm"] == 0


def test_mcp_search_shows_validity_and_status_on_norm_hit(tmp_db):
    """End-to-End: ein regulärer Suchtreffer, der als Norm redigiert wird, zeigt
    trotzdem Gültigkeitsdatum + Status -- reine Metadaten, kein Inhalt."""
    from fastapi.testclient import TestClient
    from web.main import app

    p = queries.insert_project(tmp_db, "P", "/scan")
    tmp_db.execute("UPDATE projects SET mcp_enabled=1 WHERE id=?", (p,))
    doc_id = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h1", "filename": "sia118.pdf",
        "extension": ".pdf", "filesize": 1, "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(tmp_db, doc_id, "ok")
    queries.upsert_path(tmp_db, doc_id, "/scan/sia118.pdf", True)
    queries.upsert_content(tmp_db, doc_id, "SIA 118 Allgemeine Bedingungen")
    tmp_db.execute(
        "UPDATE documents SET is_norm=1, norm_valid_from=?, norm_check_status=? WHERE id=?",
        ("2015-01-01", "veraltet", doc_id),
    )
    tmp_db.commit()

    c = TestClient(app)
    r = c.get("/api/mcp/search", params={"q": "SIA 118"})
    assert r.status_code == 200
    data = r.json()
    hit = next(h for h in data["results"] if h["id"] == doc_id)
    assert hit["norm_valid_from"] == "2015-01-01"
    assert hit["norm_check_status"] == "veraltet"
