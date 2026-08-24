"""Tests für globale Foto-Tags (photo_tags/photo_tag_assignments, /tags/*, /foto/{id}/tags)."""
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


def _jpeg_bytes():
    from PIL import Image
    import io
    im = Image.new("RGB", (60, 40), (10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


def _client():
    from fastapi.testclient import TestClient
    from web.main import app
    return TestClient(app)


def test_get_or_create_tag_is_case_insensitive_unique(tmp_db):
    id1 = queries.get_or_create_tag(tmp_db, "Rohbau")
    id2 = queries.get_or_create_tag(tmp_db, "rohbau")
    tmp_db.commit()
    assert id1 == id2


def test_assign_and_get_photo_tags(tmp_db, tmp_path):
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    doc_id = _make_photo(tmp_db, p, photo)
    tag_id = queries.get_or_create_tag(tmp_db, "Baustelle")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)
    tags = queries.get_photo_tags(tmp_db, doc_id)
    assert [t["name"] for t in tags] == ["Baustelle"]


def test_remove_photo_tag(tmp_db, tmp_path):
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    doc_id = _make_photo(tmp_db, p, photo)
    tag_id = queries.get_or_create_tag(tmp_db, "Baustelle")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)
    queries.remove_photo_tag(tmp_db, doc_id, tag_id)
    assert queries.get_photo_tags(tmp_db, doc_id) == []


def test_tag_survives_across_projects_same_document(tmp_db, tmp_path):
    """Tags hängen am Dokument (Hash) -- global, nicht an einem einzelnen Pfad."""
    p1 = queries.insert_project(tmp_db, "P1", str(tmp_path / "p1"))
    (tmp_path / "p1").mkdir()
    photo = tmp_path / "p1" / "a.jpg"
    photo.write_bytes(b"x")
    doc_id = _make_photo(tmp_db, p1, photo)
    tag_id = queries.get_or_create_tag(tmp_db, "Fassade")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)
    # Zweiter Pfad, gleiches Dokument (gleicher Hash)
    other = tmp_path / "p1" / "copy.jpg"
    other.write_bytes(b"y")
    queries.upsert_path(tmp_db, doc_id, str(other), False)
    tmp_db.commit()
    assert [t["name"] for t in queries.get_photo_tags(tmp_db, doc_id)] == ["Fassade"]


def test_endpoint_add_tag_creates_and_assigns(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(_jpeg_bytes())
    doc_id = _make_photo(tmp_db, p, photo)

    c = _client()
    r = c.post(f"/foto/{doc_id}/tags", json={"name": "Rohbau"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert [t["name"] for t in data["tags"]] == ["Rohbau"]


def test_endpoint_add_tag_missing_document_404(tmp_db):
    c = _client()
    r = c.post("/foto/999999/tags", json={"name": "Rohbau"})
    assert r.status_code == 404


def test_endpoint_add_tag_empty_name_400(tmp_db, tmp_path):
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(_jpeg_bytes())
    doc_id = _make_photo(tmp_db, p, photo)
    c = _client()
    r = c.post(f"/foto/{doc_id}/tags", json={"name": "   "})
    assert r.status_code == 400


def test_endpoint_remove_tag(tmp_db, tmp_path):
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(_jpeg_bytes())
    doc_id = _make_photo(tmp_db, p, photo)
    tag_id = queries.get_or_create_tag(tmp_db, "Rohbau")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)

    c = _client()
    r = c.delete(f"/foto/{doc_id}/tags/{tag_id}")
    assert r.status_code == 200
    assert r.json()["tags"] == []


def test_tags_suggest_matches_substring_case_insensitive(tmp_db):
    queries.get_or_create_tag(tmp_db, "Rohbau Nord")
    queries.get_or_create_tag(tmp_db, "Fassade")
    tmp_db.commit()

    c = _client()
    r = c.get("/tags/suggest", params={"q": "rohbau"})
    names = [t["name"] for t in r.json()["tags"]]
    assert names == ["Rohbau Nord"]


def test_tags_suggest_empty_query_returns_nothing(tmp_db):
    c = _client()
    r = c.get("/tags/suggest")
    assert r.json()["tags"] == []


def test_galerie_tags_lists_only_assigned_tags(tmp_db, tmp_path):
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    doc_id = _make_photo(tmp_db, p, photo)
    queries.get_or_create_tag(tmp_db, "UnbenutzterTag")  # nie zugewiesen
    tag_id = queries.get_or_create_tag(tmp_db, "Rohbau")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)

    c = _client()
    r = c.get("/galerie/tags")
    names = sorted(t["name"] for t in r.json()["tags"])
    assert names == ["Rohbau"]


def test_endpoint_get_photo_tags(tmp_db, tmp_path):
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    photo = tmp_path / "a.jpg"
    photo.write_bytes(_jpeg_bytes())
    doc_id = _make_photo(tmp_db, p, photo)
    tag_id = queries.get_or_create_tag(tmp_db, "Rohbau")
    queries.assign_photo_tag(tmp_db, doc_id, tag_id)

    c = _client()
    r = c.get(f"/foto/{doc_id}/tags")
    assert r.status_code == 200
    assert [t["name"] for t in r.json()["tags"]] == ["Rohbau"]


def test_delete_tag_globally_removes_from_all_photos(tmp_db, tmp_path):
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    a.write_bytes(_jpeg_bytes())
    b.write_bytes(_jpeg_bytes())
    doc_a = _make_photo(tmp_db, p, a, "a.jpg")
    doc_b = _make_photo(tmp_db, p, b, "b.jpg")
    tag_id = queries.get_or_create_tag(tmp_db, "Rohbau")
    queries.assign_photo_tag(tmp_db, doc_a, tag_id)
    queries.assign_photo_tag(tmp_db, doc_b, tag_id)

    c = _client()
    r = c.delete(f"/tags/{tag_id}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert queries.get_photo_tags(tmp_db, doc_a) == []
    assert queries.get_photo_tags(tmp_db, doc_b) == []
    r = c.get("/galerie/tags")
    assert r.json()["tags"] == []


def test_delete_tag_globally_unknown_id_is_noop(tmp_db):
    c = _client()
    r = c.delete("/tags/999999")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_galerie_tag_filter_global_across_projects(tmp_db, tmp_path):
    """Tag-Filter ist global -- funktioniert ohne Projektfilter, projektübergreifend."""
    (tmp_path / "p1").mkdir()
    (tmp_path / "p2").mkdir()
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p1 = queries.insert_project(tmp_db, "P1", str(tmp_path / "p1"))
    p2 = queries.insert_project(tmp_db, "P2", str(tmp_path / "p2"))
    a = tmp_path / "p1" / "a.jpg"
    b = tmp_path / "p2" / "b.jpg"
    a.write_bytes(_jpeg_bytes())
    b.write_bytes(_jpeg_bytes())
    doc_a = _make_photo(tmp_db, p1, a)
    _make_photo(tmp_db, p2, b)
    tag_id = queries.get_or_create_tag(tmp_db, "Thema-X")
    queries.assign_photo_tag(tmp_db, doc_a, tag_id)

    c = _client()
    r = c.get("/galerie", params={"tag_id": str(tag_id)})
    assert f'data-id="{doc_a}"' in r.text
    assert "b.jpg" not in r.text


def test_rename_tag_globally(tmp_db, tmp_path):
    settings._settings.setdefault("scanner", {})["base_folders"] = [{"path": str(tmp_path)}]
    p = queries.insert_project(tmp_db, "P", str(tmp_path))
    a = tmp_path / "a.jpg"
    a.write_bytes(_jpeg_bytes())
    doc_a = _make_photo(tmp_db, p, a)
    tag_id = queries.get_or_create_tag(tmp_db, "Alt")
    queries.assign_photo_tag(tmp_db, doc_a, tag_id)

    c = _client()
    r = c.post(f"/tags/{tag_id}/umbenennen", json={"name": "Neu"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    tags = queries.get_photo_tags(tmp_db, doc_a)
    assert [t["name"] for t in tags] == ["Neu"]


def test_rename_tag_rejects_clash_with_existing_name(tmp_db):
    tag_a = queries.get_or_create_tag(tmp_db, "Wichtig")
    queries.get_or_create_tag(tmp_db, "Dringend")
    tmp_db.commit()

    c = _client()
    r = c.post(f"/tags/{tag_a}/umbenennen", json={"name": "dringend"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_rename_tag_rejects_empty_name(tmp_db):
    tag_id = queries.get_or_create_tag(tmp_db, "Original")
    c = _client()
    r = c.post(f"/tags/{tag_id}/umbenennen", json={"name": "   "})
    assert r.status_code == 400
