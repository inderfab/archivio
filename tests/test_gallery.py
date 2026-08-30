"""Tests für den Foto-Browser: /galerie, /foto/{id}/thumb|ansicht|bewertung
(web/gallery.py, web/thumbnails.py)."""
from config import settings
from db import queries


def _make_photo(conn, project_id, path, filename=None):
    filename = filename or path.name
    doc_id = queries.upsert_document(conn, {
        "project_id":  project_id,
        "hash":        f"h-{path}",
        "filename":    filename,
        "extension":   ".jpg",
        "filesize":    path.stat().st_size if path.exists() else 0,
        "modified_at": "2026-01-01T00:00:00Z",
        "source_type": "filesystem",
    })
    queries.set_extraction_status(conn, doc_id, "listed")
    queries.upsert_path(conn, doc_id, str(path), True)
    conn.commit()
    return doc_id


def _jpeg_bytes(color=(120, 130, 140)):
    from PIL import Image
    import io
    im = Image.new("RGB", (60, 40), color)
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


def _client():
    from fastapi.testclient import TestClient
    from web.main import app
    return TestClient(app)


def test_galerie_empty_state(tmp_db):
    c = _client()
    r = c.get("/galerie")
    assert r.status_code == 200
    assert "Keine Fotos gefunden" in r.text


def test_galerie_lists_photo_grouped_by_project(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(_jpeg_bytes())
    p = queries.insert_project(tmp_db, "Testprojekt", str(tmp_path))
    _make_photo(tmp_db, p, photo)

    c = _client()
    r = c.get("/galerie")
    assert r.status_code == 200
    assert "Testprojekt" in r.text
    assert "photo-tile" in r.text


def test_galerie_rating_filter_excludes_unrated(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    photo_a = tmp_path / "a.jpg"
    photo_b = tmp_path / "b.jpg"
    photo_a.write_bytes(_jpeg_bytes())
    photo_b.write_bytes(_jpeg_bytes())
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    doc_a = _make_photo(tmp_db, p, photo_a)
    _make_photo(tmp_db, p, photo_b)
    queries.set_photo_rating(tmp_db, doc_a, 4)

    c = _client()
    r = c.get("/galerie", params={"sterne": "3"})
    assert f'data-id="{doc_a}"' in r.text
    assert "b.jpg" not in r.text


def test_foto_thumb_returns_real_image_for_allowed_path(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(_jpeg_bytes())
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    doc_id = _make_photo(tmp_db, p, photo)

    c = _client()
    r = c.get(f"/foto/{doc_id}/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert len(r.content) > 100


def test_foto_thumb_placeholder_for_path_outside_base_folders(tmp_db, tmp_path, monkeypatch):
    """Sicherheits-relevant: liegt der (in der DB hinterlegte) Pfad ausserhalb der
    konfigurierten NAS-Wurzelpfade, muss ein Platzhalter kommen -- niemals der
    tatsächliche Dateiinhalt eines nicht (mehr) freigegebenen Pfads."""
    outside_dir = tmp_path / "ausserhalb"
    outside_dir.mkdir()
    photo = outside_dir / "geheim.jpg"
    photo.write_bytes(_jpeg_bytes(color=(9, 9, 9)))
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path / "erlaubt")}]
    p = queries.insert_project(tmp_db, "P", str(outside_dir))
    doc_id = _make_photo(tmp_db, p, photo)

    c = _client()
    r = c.get(f"/foto/{doc_id}/thumb")
    assert r.status_code == 200
    assert r.content != _jpeg_bytes(color=(9, 9, 9))


def test_foto_thumb_placeholder_for_missing_document(tmp_db):
    c = _client()
    r = c.get("/foto/999999/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


def test_foto_bewertung_sets_and_clears_rating(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(_jpeg_bytes())
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    doc_id = _make_photo(tmp_db, p, photo)

    c = _client()
    r = c.post(f"/foto/{doc_id}/bewertung", json={"rating": 4})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "rating": 4}
    assert queries.get_photo_rating(tmp_db, doc_id) == 4

    r = c.post(f"/foto/{doc_id}/bewertung", json={"rating": 0})
    assert r.json()["rating"] == 0
    assert queries.get_photo_rating(tmp_db, doc_id) == 0


def test_foto_bewertung_rejects_out_of_range(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(_jpeg_bytes())
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    doc_id = _make_photo(tmp_db, p, photo)

    c = _client()
    r = c.post(f"/foto/{doc_id}/bewertung", json={"rating": 6})
    assert r.status_code == 400


def test_foto_bewertung_unknown_document_404(tmp_db):
    c = _client()
    r = c.post("/foto/999999/bewertung", json={"rating": 3})
    assert r.status_code == 404


def test_galerie_format_filter(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    jpg = tmp_path / "a.jpg"
    png = tmp_path / "b.png"
    jpg.write_bytes(_jpeg_bytes())
    png.write_bytes(_jpeg_bytes())
    doc_jpg = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-jpg", "filename": "a.jpg", "extension": ".jpg",
        "filesize": 10, "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_jpg, str(jpg), True)
    queries.set_extraction_status(tmp_db, doc_jpg, "listed")
    doc_png = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-png", "filename": "b.png", "extension": ".png",
        "filesize": 10, "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_png, str(png), True)
    queries.set_extraction_status(tmp_db, doc_png, "listed")
    tmp_db.commit()

    c = _client()
    r = c.get("/galerie", params={"formate": "png"})
    assert f'data-id="{doc_png}"' in r.text
    assert "a.jpg" not in r.text


def test_galerie_date_filter(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    old = tmp_path / "old.jpg"
    new = tmp_path / "new.jpg"
    old.write_bytes(_jpeg_bytes())
    new.write_bytes(_jpeg_bytes())
    doc_old = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-old", "filename": "old.jpg", "extension": ".jpg",
        "filesize": 10, "modified_at": "2020-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_old, str(old), True)
    queries.set_extraction_status(tmp_db, doc_old, "listed")
    doc_new = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-new", "filename": "new.jpg", "extension": ".jpg",
        "filesize": 10, "modified_at": "2026-06-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_new, str(new), True)
    queries.set_extraction_status(tmp_db, doc_new, "listed")
    tmp_db.commit()

    c = _client()
    r = c.get("/galerie", params={"date_from": "2025-01-01"})
    assert f'data-id="{doc_new}"' in r.text
    assert "old.jpg" not in r.text


def test_galerie_ordner_tags_and_filter(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    (tmp_path / "BaustelleA").mkdir()
    (tmp_path / "BaustelleB").mkdir()
    a = tmp_path / "BaustelleA" / "a.jpg"
    b = tmp_path / "BaustelleB" / "b.jpg"
    a.write_bytes(_jpeg_bytes())
    b.write_bytes(_jpeg_bytes())
    doc_a = _make_photo(tmp_db, p, a)
    _make_photo(tmp_db, p, b)

    c = _client()
    r = c.get("/galerie/ordner-tags", params={"project_id": str(p)})
    assert sorted(r.json()["tags"]) == ["BaustelleA", "BaustelleB"]

    r = c.get("/galerie", params={"project_id": str(p), "ordner": "BaustelleA"})
    assert f'data-id="{doc_a}"' in r.text
    assert "b.jpg" not in r.text


def test_galerie_ordner_filter_handles_nfd_nfc_mismatch(tmp_db, tmp_path):
    """macOS liefert Ordnernamen mit Umlauten beim Scan oft NFD-zerlegt (z.B. 'u' +
    kombinierender Trema statt 'ü'), waehrend eine Browser-Eingabe/ein angeklickter Chip
    ueblicherweise NFC-komponiert ist -- sichtbar identischer Name, andere Byte-Folge.
    Ohne Normalisierung (db/connection.py::_nfc) liefert weder die Tag-Liste den
    richtigen Namen noch der Ordner-Filter einen Treffer."""
    import unicodedata

    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    nfd_name = unicodedata.normalize("NFD", "Südfassade")
    assert nfd_name != "Südfassade"  # sonst testet dieser Test nichts
    folder = tmp_path / nfd_name
    folder.mkdir()
    photo = folder / "a.jpg"
    photo.write_bytes(_jpeg_bytes())
    doc_id = _make_photo(tmp_db, p, photo)

    c = _client()
    r = c.get("/galerie/ordner-tags", params={"project_id": str(p)})
    assert r.json()["tags"] == ["Südfassade"]  # NFC in der Anzeige, nicht das rohe NFD

    r = c.get("/galerie", params={"project_id": str(p), "ordner": "Südfassade"})
    assert f'data-id="{doc_id}"' in r.text


def test_galerie_ordner_tags_excludes_deleted_folder(tmp_db, tmp_path):
    """Ordner koennen nach dem Scan geloescht werden (die DB behaelt die Dokumente
    absichtlich, siehe _fetch_folder_tags-Kommentar) -- der Ordner-Filter soll einen
    inzwischen verschwundenen Ordner trotzdem nicht mehr ewig zur Auswahl anbieten."""
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    (tmp_path / "Aktuell").mkdir()
    (tmp_path / "Geloescht").mkdir()
    a = tmp_path / "Aktuell" / "a.jpg"
    b = tmp_path / "Geloescht" / "b.jpg"
    a.write_bytes(_jpeg_bytes())
    b.write_bytes(_jpeg_bytes())
    _make_photo(tmp_db, p, a)
    _make_photo(tmp_db, p, b)

    import shutil
    shutil.rmtree(tmp_path / "Geloescht")

    c = _client()
    r = c.get("/galerie/ordner-tags", params={"project_id": str(p)})
    assert r.json()["tags"] == ["Aktuell"]


def test_galerie_ordner_tags_empty_without_project(tmp_db):
    c = _client()
    r = c.get("/galerie/ordner-tags")
    assert r.json()["tags"] == []


def test_galerie_hides_files_over_default_15mb(tmp_db, tmp_path):
    """Standardfilter (15 MB) gilt fuer ALLE Formate, nicht nur TIFF -- frueher
    war nur TIFF ueberhaupt begrenzt (auf 20 MB), alle anderen Formate liefen
    ungefiltert und wurden trotzdem oft nicht angezeigt (Pillow Decompression-
    Bomb-Schutz bei hochaufgeloesten Planscans, siehe web/thumbnails.py)."""
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    small = tmp_path / "small.jpg"
    big = tmp_path / "big.jpg"
    small.write_bytes(_jpeg_bytes())
    big.write_bytes(_jpeg_bytes())
    doc_small = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-small", "filename": "small.jpg", "extension": ".jpg",
        "filesize": 1024, "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_small, str(small), True)
    queries.set_extraction_status(tmp_db, doc_small, "listed")
    doc_big = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-big", "filename": "big.jpg", "extension": ".jpg",
        "filesize": 16 * 1024 * 1024, "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_big, str(big), True)
    queries.set_extraction_status(tmp_db, doc_big, "listed")
    tmp_db.commit()

    c = _client()
    r = c.get("/galerie")
    assert f'data-id="{doc_small}"' in r.text
    assert "big.jpg" not in r.text


def test_galerie_max_size_mb_alle_includes_large_files(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    big = tmp_path / "big.jpg"
    big.write_bytes(_jpeg_bytes())
    doc_big = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-big", "filename": "big.jpg", "extension": ".jpg",
        "filesize": 16 * 1024 * 1024, "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_big, str(big), True)
    queries.set_extraction_status(tmp_db, doc_big, "listed")
    tmp_db.commit()

    c = _client()
    r = c.get("/galerie", params={"max_size_mb": "alle"})
    assert f'data-id="{doc_big}"' in r.text


def test_galerie_max_size_mb_custom_threshold(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    mid = tmp_path / "mid.jpg"
    mid.write_bytes(_jpeg_bytes())
    doc_mid = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-mid", "filename": "mid.jpg", "extension": ".jpg",
        "filesize": 8 * 1024 * 1024, "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_mid, str(mid), True)
    queries.set_extraction_status(tmp_db, doc_mid, "listed")
    tmp_db.commit()

    c = _client()
    assert f'data-id="{doc_mid}"' not in c.get("/galerie", params={"max_size_mb": "5"}).text
    assert f'data-id="{doc_mid}"' in c.get("/galerie", params={"max_size_mb": "10"}).text


def test_galerie_sort_name_az_and_za(tmp_db, tmp_path):
    """Alphabetische Sortierung pro Ordner (z.B. '1_xx.jpg', '2_xx.jpg') -- vorher
    war die Galerie nur nach Datum sortierbar, was solche Reihenfolgen 'durcheinander'
    erscheinen liess, sobald mehrere Dateien denselben modified_at-Zeitstempel hatten."""
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    for name in ("2_b.jpg", "1_a.jpg", "10_c.jpg"):
        photo = tmp_path / name
        photo.write_bytes(_jpeg_bytes())
        _make_photo(tmp_db, p, photo)

    c = _client()
    r_az = c.get("/galerie", params={"sort": "name_az"})
    pos_az = {name: r_az.text.index(name) for name in ("1_a.jpg", "10_c.jpg", "2_b.jpg")}
    assert pos_az["1_a.jpg"] < pos_az["2_b.jpg"] < pos_az["10_c.jpg"]

    r_za = c.get("/galerie", params={"sort": "name_za"})
    pos_za = {name: r_za.text.index(name) for name in ("1_a.jpg", "10_c.jpg", "2_b.jpg")}
    assert pos_za["10_c.jpg"] < pos_za["2_b.jpg"] < pos_za["1_a.jpg"]


def test_galerie_sort_datum_alt_zuerst(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    older = tmp_path / "older.jpg"
    newer = tmp_path / "newer.jpg"
    older.write_bytes(_jpeg_bytes())
    newer.write_bytes(_jpeg_bytes())
    doc_older = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-older", "filename": "older.jpg", "extension": ".jpg",
        "filesize": older.stat().st_size, "modified_at": "2020-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_older, str(older), True)
    queries.set_extraction_status(tmp_db, doc_older, "listed")
    doc_newer = queries.upsert_document(tmp_db, {
        "project_id": p, "hash": "h-newer", "filename": "newer.jpg", "extension": ".jpg",
        "filesize": newer.stat().st_size, "modified_at": "2026-01-01T00:00:00Z", "source_type": "filesystem",
    })
    queries.upsert_path(tmp_db, doc_newer, str(newer), True)
    queries.set_extraction_status(tmp_db, doc_newer, "listed")
    tmp_db.commit()

    c = _client()
    r = c.get("/galerie", params={"sort": "datum_alt"})
    assert r.text.index("older.jpg") < r.text.index("newer.jpg")


def test_kontaktbogen_shows_selected_photos_in_order(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(_jpeg_bytes())
    b.write_bytes(_jpeg_bytes())
    doc_a = _make_photo(tmp_db, p, a)
    doc_b = _make_photo(tmp_db, p, b)
    queries.set_photo_rating(tmp_db, doc_b, 3)

    c = _client()
    r = c.get("/galerie/kontaktbogen", params={"ids": f"{doc_b},{doc_a}"})
    assert r.status_code == 200
    assert "2 Fotos" in r.text
    pos_b = r.text.index(f"/foto/{doc_b}/ansicht")
    pos_a = r.text.index(f"/foto/{doc_a}/ansicht")
    assert pos_b < pos_a  # Auswahlreihenfolge (b zuerst) erhalten
    assert "★★★☆☆" in r.text


def test_kontaktbogen_empty_selection(tmp_db):
    c = _client()
    r = c.get("/galerie/kontaktbogen")
    assert r.status_code == 200
    assert "Keine Fotos ausgewählt" in r.text


def test_kontaktbogen_ignores_invalid_ids(tmp_db):
    c = _client()
    r = c.get("/galerie/kontaktbogen", params={"ids": "abc,,999999"})
    assert r.status_code == 200
    assert "0 Fotos" in r.text
