"""
Extraktoren pro Dateityp. Jeder Extraktor nimmt einen Path und gibt (text, language) zurück.
Bei nicht unterstützten Typen: raise UnsupportedFormat.
Bei Fehler in der Extraktion: raise ExtractionError.

SICHERHEIT: Alle Extraktoren öffnen Dateien ausschliesslich lesend.
Verboten: os.remove, os.rename, shutil.delete, open(..., 'w'), open(..., 'wb').
"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


class UnsupportedFormat(Exception):
    pass


class ExtractionError(Exception):
    pass


# ── Typographische Normalisierung ─────────────────────────────────────────────
# OCR-PDFs enthalten oft Unicode-Ligaturen (ﬂ ﬁ ﬀ ﬃ ﬄ) anstelle normaler
# Buchstaben. "Geschossﬂäche" (mit U+FB02) trifft kein LIKE '%geschossfläche%'.

_LIGATURE_TABLE = str.maketrans({
    'ﬀ': 'ff',   # ﬀ LATIN SMALL LIGATURE FF
    'ﬁ': 'fi',   # ﬁ LATIN SMALL LIGATURE FI
    'ﬂ': 'fl',   # ﬂ LATIN SMALL LIGATURE FL
    'ﬃ': 'ffi',  # ﬃ LATIN SMALL LIGATURE FFI
    'ﬄ': 'ffl',  # ﬄ LATIN SMALL LIGATURE FFL
    'ﬅ': 'st',   # ﬅ LATIN SMALL LIGATURE LONG S T
    'ﬆ': 'st',   # ﬆ LATIN SMALL LIGATURE ST
    'ʼ': "'",    # ʼ MODIFIER LETTER APOSTROPHE (häufig in OCR)
    '’': "'",    # ' RIGHT SINGLE QUOTATION MARK
})


# OCR-Leerzeichen nach Ligatur-Zeichenfolgen:
# "Geschossfl äche" → "Geschossfläche"  (ﬂ wurde zu fl, dann Leerzeichen)
# "Aufl age" → "Auflage"
# "Defi nition" → "Definition"
# NUR nach den Ligatur-Buchstaben-Paaren (fl, fi, ff, ffi, ffl) — nicht generell,
# sonst würden echte Wortlücken wie "ein neues" zu "einneues".
_OCR_LIGATURE_SPACE = re.compile(r'(fl|fi|ff|ffi|ffl) (?=[a-zäöüß])')

# Mehrfach-Leerzeichen die OCR manchmal erzeugt ("Defi  nition")
_OCR_MULTI_SPACE = re.compile(r' {2,}')


def normalize_text(text: str) -> str:
    """Ligaturen und OCR-Leerzeichen-Artefakte normalisieren. Umlaute bleiben erhalten.

    Fixes:
    - ﬂ → fl, ﬁ → fi, ﬀ → ff, ﬃ → ffi, ﬄ → ffl  (Unicode-Ligaturen)
    - "Geschossfl äche" → "Geschossfläche"  (Leerzeichen nach Ligatur-Zeichenpaar)
    - "Aufl age" → "Auflage"
    - "Defi  nition" → "Definition"
    """
    text = text.translate(_LIGATURE_TABLE)
    text = _OCR_MULTI_SPACE.sub(' ', text)
    text = _OCR_LIGATURE_SPACE.sub(r'\1', text)
    return text


# ── TXT / MD ──────────────────────────────────────────────────────────────────

def extract_txt(path: Path) -> tuple[str, str]:
    # READ-ONLY: NAS darf nie verändert werden
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return path.read_text(encoding=enc), ""
        except UnicodeDecodeError:
            continue
    raise ExtractionError(f"Cannot decode {path}")


# ── PDF ───────────────────────────────────────────────────────────────────────

def _pdf_pages_pymupdf(path: Path) -> list[dict]:
    """Primäre PDF-Extraktion via PyMuPDF — besser bei custom Fonts.

    Flags: Ligaturen werden NICHT beibehalten (decomponiert zu fl/fi/ff/…),
    Zeilenenden werden verbunden (DEHYPHENATE). So entsteht suchbarer Klartext.
    """
    import fitz  # pymupdf
    # TEXT_PRESERVE_LIGATURES=1 weglassen → ﬂ→fl etc. direkt beim Extrahieren
    # TEXT_PRESERVE_WHITESPACE=2 beibehalten
    # TEXT_DEHYPHENATE=16 → Wörter mit Silbentrennung am Zeilenende zusammenführen
    FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE
    result = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text(flags=FLAGS).strip()
            if len(text) < 50:
                continue
            result.append({"page_number": i, "content": text})
    return result


def _pdf_pages_pypdf(path: Path) -> list[dict]:
    """Fallback-Extraktion via pypdf."""
    import pypdf
    reader = pypdf.PdfReader(str(path))
    result = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if len(text) < 50:
            continue
        result.append({"page_number": i, "content": text})
    return result


def _pdf_pages_ocr(path: Path) -> list[dict]:
    """OCR-Fallback via PyMuPDF + Tesseract.

    Wird verwendet wenn das PDF eine unleserliche Font-Kodierung hat (Mojibake).
    full=True → komplette Seite OCR, ignoriert den unleserlichen Text-Layer.
    Versucht Deutsch, fällt auf Englisch zurück falls Sprachpaket fehlt.
    """
    import fitz
    FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE
    result = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            text = ""
            for lang in ("deu+eng", "deu", "eng"):
                try:
                    tp = page.get_textpage_ocr(
                        flags=FLAGS,
                        language=lang,
                        dpi=150,
                        full=True,   # Gesamte Seite neu OCR — ignoriert unlesbaren Text-Layer
                    )
                    text = page.get_text(textpage=tp, flags=FLAGS).strip()
                    if text:
                        break
                except Exception:
                    continue
            if len(text) < 50:
                continue
            result.append({"page_number": i, "content": text})
    return result


def _has_mojibake(pages: list[dict]) -> bool:
    """True wenn >30 % der Zeichen Replacement-Characters sind (Encoding-Fehler)."""
    if not pages:
        return False
    sample = "".join(p["content"] for p in pages[:5])
    if not sample:
        return False
    ratio = sample.count("�") / len(sample)
    return ratio > 0.30


def extract_pdf(path: Path) -> tuple[str, str]:
    pages = extract_pdf_pages(path)
    return "\n".join(p["content"] for p in pages), ""


def extract_pdf_pages(path: Path) -> list[dict]:
    """Gibt [{page_number, content}] zurück.

    Reihenfolge:
    1. PyMuPDF (schnell, gute Font-Unterstützung)
    2. PyMuPDF + OCR via Tesseract (Fallback bei unlesbaren Fonts / Mojibake)
    3. pypdf (letzter Ausweg)
    """
    # Schritt 1: PyMuPDF Text-Extraktion
    try:
        pages = _pdf_pages_pymupdf(path)
        if not _has_mojibake(pages):
            return pages
        # Font-Encoding unleserlich → OCR versuchen
        log.info("Mojibake erkannt, versuche OCR-Fallback: %s", path.name)
    except ImportError:
        pass  # pymupdf nicht installiert → direkt zu pypdf
    except Exception as pymupdf_exc:
        # PyMuPDF-Exception (z.B. korruptes PDF, unbekanntes Format) → OCR versuchen
        log.info("PyMuPDF-Fehler, versuche OCR-Fallback (%s): %s", path.name, pymupdf_exc)
        try:
            ocr_pages = _pdf_pages_ocr(path)
            if ocr_pages and not _has_mojibake(ocr_pages):
                log.info("OCR-Fallback nach PyMuPDF-Fehler erfolgreich: %s", path.name)
                return ocr_pages
        except Exception as ocr_exc:
            log.warning("OCR-Fallback fehlgeschlagen (%s): %s", path.name, ocr_exc)
        raise ExtractionError(str(pymupdf_exc)) from pymupdf_exc
    else:
        # Schritt 2: OCR via Tesseract (nach Mojibake)
        try:
            ocr_pages = _pdf_pages_ocr(path)
            if ocr_pages and not _has_mojibake(ocr_pages):
                log.info("OCR-Fallback erfolgreich: %s (%d Seiten)", path.name, len(ocr_pages))
                return ocr_pages
            log.warning("OCR liefert weiterhin Mojibake: %s", path.name)
        except Exception as ocr_exc:
            log.warning("OCR-Fallback fehlgeschlagen (%s): %s", path.name, ocr_exc)

    # Schritt 3: pypdf
    try:
        import pypdf  # noqa: F401
    except ImportError:
        raise UnsupportedFormat("Weder pymupdf noch pypdf installiert")
    try:
        return _pdf_pages_pypdf(path)
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


CHUNK_SIZE    = 800   # Zeichen pro Chunk
CHUNK_OVERLAP = 120  # Überlapp zwischen benachbarten Chunks


def _split_long_text(page_number, text: str, max_len: int = 3000) -> list[dict]:
    """PDF-seitenweises Splitting — rekursiv bis alle Teile ≤ max_len Zeichen."""
    if len(text) <= max_len:
        return [{"page_number": page_number, "content": text}]
    mid = len(text) // 2
    split_at = text.rfind(" ", max(0, mid - 300), min(len(text), mid + 300))
    if split_at <= 0:
        split_at = mid
    left  = text[:split_at].strip()
    right = text[split_at:].strip()
    return (
        _split_long_text(page_number, left,  max_len) +
        _split_long_text(page_number, right, max_len)
    )


def split_text_into_chunks(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Teilt Text in überlappende Chunks auf. Trennt bevorzugt an Absatz- oder Wortgrenzen."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start  = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:].strip())
            break
        # Absatzgrenze bevorzugen
        split_at = text.rfind("\n\n", start, end)
        if split_at == -1 or split_at <= start:
            # Zeilenumbruch
            split_at = text.rfind("\n", start + chunk_size // 2, end)
        if split_at == -1 or split_at <= start:
            # Wortgrenze
            split_at = text.rfind(" ", start + chunk_size // 2, end)
        if split_at == -1 or split_at <= start:
            split_at = end
        chunk = text[start:split_at].strip()
        if chunk:
            chunks.append(chunk)
        start = split_at - overlap
        if start < 0:
            start = 0
    return [c for c in chunks if len(c) > 20]


def extract_chunks(path: Path) -> list[dict]:
    """Gibt [{page_number, chunk_index, content}] zurück.

    PDF: ein Chunk pro Seite (lange Seiten werden gesplittet).
    Andere Formate: Text wird in überlappende Chunks von ~800 Zeichen aufgeteilt.
    Alle Texte werden normalisiert (Ligaturen → reguläre Zeichen).
    """
    ext = path.suffix.lower()
    if ext == ".pdf":
        pages = extract_pdf_pages(path)
        chunks = []
        idx = 0
        for page_data in pages:
            for c in _split_long_text(page_data["page_number"], page_data["content"]):
                chunks.append({
                    "page_number": c["page_number"],
                    "chunk_index": idx,
                    "content":     normalize_text(c["content"]),
                })
                idx += 1
        return chunks
    else:
        text, _ = extract(path)
        if not text:
            return []
        parts = split_text_into_chunks(normalize_text(text))
        return [
            {"page_number": None, "chunk_index": i, "content": part}
            for i, part in enumerate(parts)
        ]


# ── DOCX ──────────────────────────────────────────────────────────────────────

def extract_docx(path: Path) -> tuple[str, str]:
    try:
        import docx
    except ImportError:
        raise UnsupportedFormat("python-docx not installed")
    try:
        # READ-ONLY: NAS darf nie verändert werden — Document() öffnet nur lesend
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text), ""
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


# ── DOC (Word 97-2003) ────────────────────────────────────────────────────────

def extract_doc(path: Path) -> tuple[str, str]:
    """Nutzt macOS-integriertes textutil; kein extra Paket nötig."""
    import subprocess
    try:
        # READ-ONLY: NAS darf nie verändert werden — textutil liest nur
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise ExtractionError(result.stderr.strip() or "textutil fehlgeschlagen")
        return result.stdout, ""
    except FileNotFoundError:
        raise UnsupportedFormat("textutil nicht verfügbar (nur macOS)")
    except subprocess.TimeoutExpired:
        raise ExtractionError(f"textutil Timeout: {path}")


# ── XLSX ──────────────────────────────────────────────────────────────────────

_XLSX_MAX_MB      = 20       # Dateien grösser als 20 MB überspringen
_XLSX_MAX_SHEETS  = 10       # Maximal 10 Arbeitsblätter
_XLSX_MAX_ROWS    = 5_000    # Maximal 5000 Zeilen pro Blatt
_XLSX_TIMEOUT_SEC = 60       # Abbruch nach 60 Sekunden


def extract_xlsx(path: Path) -> tuple[str, str]:
    try:
        import openpyxl
    except ImportError:
        raise UnsupportedFormat("openpyxl not installed")

    # Grössencheck — sehr grosse XLSX-Dateien hängen openpyxl auf
    size_mb = path.stat().st_size / 1_048_576
    if size_mb > _XLSX_MAX_MB:
        raise ExtractionError(
            f"XLSX zu gross ({size_mb:.1f} MB > {_XLSX_MAX_MB} MB) — übersprungen"
        )

    # Extraktion in separatem Thread mit Timeout
    import threading
    result_box: list = []
    error_box:  list = []

    def _extract():
        try:
            # READ-ONLY: NAS darf nie verändert werden — read_only=True
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            parts = []
            for sheet in wb.worksheets[:_XLSX_MAX_SHEETS]:
                row_count = 0
                for row in sheet.iter_rows(values_only=True):
                    line = "  ".join(
                        str(c)[:200] for c in row   # Zellinhalt auf 200 Zeichen begrenzen
                        if c is not None and str(c).strip()
                    )
                    if line:
                        parts.append(line)
                    row_count += 1
                    if row_count >= _XLSX_MAX_ROWS:
                        parts.append(f"[… gekürzt nach {_XLSX_MAX_ROWS} Zeilen]")
                        break
            wb.close()
            result_box.append("\n".join(parts))
        except Exception as exc:
            error_box.append(exc)

    t = threading.Thread(target=_extract, daemon=True)
    t.start()
    t.join(timeout=_XLSX_TIMEOUT_SEC)

    if t.is_alive():
        raise ExtractionError(
            f"XLSX Timeout nach {_XLSX_TIMEOUT_SEC}s — Datei zu komplex: {path.name}"
        )
    if error_box:
        raise ExtractionError(str(error_box[0]))
    return result_box[0] if result_box else ("", "")


# ── EML ───────────────────────────────────────────────────────────────────────

def extract_eml(path: Path) -> tuple[str, str]:
    import email
    from email import policy
    try:
        # READ-ONLY: NAS darf nie verändert werden — öffnet nur mit 'rb'
        with open(path, "rb") as f:
            msg = email.message_from_binary_file(f, policy=policy.default)

        parts = []
        subject = msg.get("subject", "")
        sender  = msg.get("from", "")
        if subject:
            parts.append(f"Betreff: {subject}")
        if sender:
            parts.append(f"Von: {sender}")

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))

        return "\n".join(parts), ""
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


# ── RTF ───────────────────────────────────────────────────────────────────────

def extract_rtf(path: Path) -> tuple[str, str]:
    try:
        from striprtf.striprtf import rtf_to_text
    except ImportError:
        raise UnsupportedFormat("striprtf not installed")
    try:
        # READ-ONLY: NAS darf nie verändert werden
        for enc in ("utf-8", "latin-1", "cp1252"):
            try:
                raw = path.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ExtractionError(f"Cannot decode RTF: {path}")
        return rtf_to_text(raw), ""
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


# ── Registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, callable] = {
    ".txt":  extract_txt,
    ".md":   extract_txt,
    ".pdf":  extract_pdf,
    ".docx": extract_docx,
    ".doc":  extract_doc,
    ".xlsx": extract_xlsx,
    ".eml":  extract_eml,
    ".rtf":  extract_rtf,
}


def extract(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    fn = _REGISTRY.get(ext)
    if fn is None:
        raise UnsupportedFormat(f"No extractor for {ext}")
    return fn(path)
