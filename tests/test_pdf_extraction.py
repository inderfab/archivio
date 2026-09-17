"""Tests für die PDF-Extraktion (scanner/extractors.py) -- pypdfium2 als Ersatz
für PyMuPDF (Lizenzwechsel: PyMuPDF ist AGPL-3.0, für den kommerziellen,
closed-source Vertrieb von Archivio nicht ohne kostenpflichtige Zusatzlizenz
nutzbar; pypdfium2 ist Apache-2.0/BSD-lizenziert). pypdf bleibt als Fallback,
OCR läuft über pypdfium2-Rendering + pytesseract (Apache-2.0) statt über
PyMuPDFs eingebaute Tesseract-Bridge.

Testdateien werden selbst gebaut (Handschrift-PDF mit korrekter xref-Tabelle
bzw. ein von Pillow erzeugtes bildbasiertes PDF) -- bewusst OHNE eine
PDF-Bibliothek zu verwenden, die selbst Gegenstand des Tests ist."""
import shutil

import pytest
from PIL import Image, ImageDraw


def _make_text_pdf(path, text="Hello Archivio Test", creator=None, producer=None):
    """Minimales, aber valides PDF von Hand (korrekte xref-Tabelle + optionales
    /Info-Objekt für Creator/Producer)."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 300 300] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 14 Tf 20 150 Td ({text}) Tj ET".encode()
    objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")

    info_idx = None
    if creator or producer:
        parts = []
        if creator:
            parts.append(f"/Creator ({creator})")
        if producer:
            parts.append(f"/Producer ({producer})")
        objs.append(("<< " + " ".join(parts) + " >>").encode())
        info_idx = len(objs)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    xref_offset = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    trailer = f"<< /Size {n} /Root 1 0 R"
    if info_idx:
        trailer += f" /Info {info_idx} 0 R"
    trailer += " >>\n"
    out += b"trailer\n" + trailer.encode()
    out += b"startxref\n" + f"{xref_offset}\n".encode()
    out += b"%%EOF"
    path.write_bytes(bytes(out))


def _make_annotated_pdf(path, annotation_text, page_text=""):
    """PDF mit einer FreeText-Annotation (/Annots -> /Contents) -- simuliert eine
    Bauleitungs-Rückmeldung/Markup auf einem Planausschnitt. page_text leer lässt
    den Content-Stream bewusst text-frei, damit ein Test nur den Annotations-Pfad
    prüft (siehe _page_annotation_text in scanner/extractors.py). Von Hand gebaut
    wie _make_text_pdf, aus demselben Grund (keine PDF-Bibliothek verwenden, die
    selbst Gegenstand des Tests ist)."""
    stream = f"BT /F1 14 Tf 20 150 Td ({page_text}) Tj ET".encode() if page_text else b""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 300 300] /Contents 5 0 R /Annots [6 0 R] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ("<< /Type /Annot /Subtype /FreeText /Rect [0 0 100 20] /Contents ("
         + annotation_text + ") >>").encode(),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj".encode() + body + b"endobj\n"
    xref_offset = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(n).encode() + b" /Root 1 0 R >>\n"
    out += b"startxref\n" + f"{xref_offset}\n".encode()
    out += b"%%EOF"
    path.write_bytes(bytes(out))


def _make_scanned_pdf(path, text):
    """Bildbasiertes PDF ohne Textebene -- simuliert einen Scan. Pillow (bereits
    Abhängigkeit für die Foto-Galerie) kann direkt als PDF speichern."""
    img = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 80), text, fill="black")
    img.save(str(path), "PDF", resolution=150.0)


_HAS_TESSERACT = shutil.which("tesseract") is not None


def test_extract_pdf_pages_reads_real_text_via_pypdfium2(tmp_path):
    """content muss mindestens 50 Zeichen haben, sonst filtert extract_pdf_pages
    die Seite als zu kurz heraus (siehe len(text) >= 50 in scanner/extractors.py)
    -- das gilt für jede Extraktionsstufe gleichermassen, nicht nur pypdfium2."""
    from scanner import extractors

    pdf_path = tmp_path / "text.pdf"
    _make_text_pdf(pdf_path, "Grundriss Erdgeschoss mit Wohnflaeche 120 Quadratmeter Total")

    pages = extractors.extract_pdf_pages(pdf_path)
    assert len(pages) == 1
    assert pages[0]["page_number"] == 1
    assert "Grundriss Erdgeschoss" in pages[0]["content"]


def test_extract_pdf_pages_includes_annotation_text(tmp_path):
    """PyMuPDF (vorher) hat Markup-Kommentare automatisch mitgelesen, pypdfium2s
    Textebene und pypdf.extract_text() beide nicht (siehe _page_annotation_text)
    -- Regression für den realen Fall: Planrand-Kommentare aus einem echten
    "Rückmeldung"-PDF (Bauleitungs-Markup) gingen beim Umbau auf pypdfium2
    sonst verloren, obwohl der Text im PDF bereits sauber vorliegt."""
    from scanner import extractors

    pdf_path = tmp_path / "annotated.pdf"
    _make_annotated_pdf(pdf_path, "Eingang Trafo muss hier bleiben, Anschluss pruefen bitte")

    pages = extractors.extract_pdf_pages(pdf_path)
    assert len(pages) == 1
    assert "Eingang Trafo muss hier bleiben" in pages[0]["content"]


def test_extract_pdf_pages_merges_page_text_and_annotations(tmp_path):
    """Seiten mit echtem Textinhalt UND Kommentaren müssen beides behalten,
    nicht nur eines von beiden."""
    from scanner import extractors

    pdf_path = tmp_path / "annotated2.pdf"
    _make_annotated_pdf(
        pdf_path,
        "Kommentar der Bauleitung zur Fassadenoeffnung im Erdgeschoss",
        page_text="Grundriss Erdgeschoss mit Wohnflaeche und weiteren Angaben hier",
    )

    pages = extractors.extract_pdf_pages(pdf_path)
    assert len(pages) == 1
    content = pages[0]["content"]
    assert "Grundriss Erdgeschoss" in content
    assert "Kommentar der Bauleitung" in content


def test_extract_pdf_wraps_pages_into_single_text(tmp_path):
    from scanner import extractors

    pdf_path = tmp_path / "text.pdf"
    _make_text_pdf(pdf_path, "Baubeschrieb fuer das Projekt an der Musterstrasse in Musterhausen")

    text, _ = extractors.extract_pdf(pdf_path)
    assert "Baubeschrieb" in text


def test_extract_chunks_pdf_produces_one_chunk_per_page(tmp_path):
    from scanner import extractors

    pdf_path = tmp_path / "text.pdf"
    _make_text_pdf(pdf_path, "Text lang genug fuer die Mindestlaenge von fuenfzig Zeichen hier")

    chunks = extractors.extract_chunks(pdf_path)
    assert len(chunks) == 1
    assert chunks[0]["page_number"] == 1
    assert "Mindestlaenge" in chunks[0]["content"]


def test_extract_pdf_metadata_reads_creator_and_producer(tmp_path):
    from scanner import extractors

    pdf_path = tmp_path / "plan.pdf"
    _make_text_pdf(pdf_path, "Grundriss", creator="ArchiCAD 26", producer="GRAPHISOFT PDF")

    meta = extractors.extract_pdf_metadata(pdf_path)
    assert meta["creator_app"] == "archicad"
    assert meta["is_plan"] is True


def test_extract_pdf_metadata_no_plan_app_when_absent(tmp_path):
    from scanner import extractors

    pdf_path = tmp_path / "doc.pdf"
    _make_text_pdf(pdf_path, "Normaler Text", creator="Microsoft Word", producer="Word PDF")

    meta = extractors.extract_pdf_metadata(pdf_path)
    assert meta["creator_app"] == ""
    assert meta["is_plan"] is False


def test_extract_pdf_pages_falls_back_to_pypdf_when_pypdfium2_missing(tmp_path, monkeypatch):
    """Wenn pypdfium2 nicht importierbar ist, muss pypdf (Tier 2) übernehmen --
    simuliert über sys.modules, ohne das Paket tatsächlich zu deinstallieren."""
    import sys

    from scanner import extractors

    monkeypatch.setitem(sys.modules, "pypdfium2", None)  # import pypdfium2 -> ImportError

    pdf_path = tmp_path / "text.pdf"
    _make_text_pdf(pdf_path, "Fallback Text ueber pypdf statt pypdfium2 fuer diesen Testfall hier")

    pages = extractors.extract_pdf_pages(pdf_path)
    assert len(pages) == 1
    assert "Fallback Text" in pages[0]["content"]


@pytest.mark.skipif(not _HAS_TESSERACT, reason="Tesseract-OCR ist auf dieser Maschine nicht installiert")
def test_extract_pdf_pages_uses_ocr_for_image_only_pdf(tmp_path):
    """Ein bildbasiertes PDF ohne Textebene (wie ein echter Scan) muss über die
    dritte Stufe (pypdfium2-Rendering + pytesseract) lesbar werden."""
    from scanner import extractors

    pdf_path = tmp_path / "scan.pdf"
    _make_scanned_pdf(pdf_path, "Dies ist ein gescanntes Testdokument fuer OCR Erkennung heute")

    pages = extractors.extract_pdf_pages(pdf_path)
    assert len(pages) == 1
    # OCR ist nie pixelgenau -- nur ein prägnantes Teilwort prüfen, nicht den ganzen Satz.
    assert "gescanntes" in pages[0]["content"].lower() or "testdokument" in pages[0]["content"].lower()


def test_ocr_pages_returns_empty_for_blank_page(tmp_path):
    """Eine leere (weisse) Seite darf nicht als 'text gefunden' durchgehen --
    OCR liefert dafür nichts oder nur Rauschen unter der 50-Zeichen-Schwelle."""
    from scanner import extractors

    img = Image.new("RGB", (300, 300), "white")
    pdf_path = tmp_path / "blank.pdf"
    img.save(str(pdf_path), "PDF")

    pages = extractors._ocr_pages(str(pdf_path))
    assert pages == []
