"""
Extraktoren pro Dateityp. Jeder Extraktor nimmt einen Path und gibt (text, language) zurück.
Bei nicht unterstützten Typen: raise UnsupportedFormat.
Bei Fehler in der Extraktion: raise ExtractionError.

SICHERHEIT: Alle Extraktoren öffnen Dateien ausschliesslich lesend.
Verboten: os.remove, os.rename, shutil.delete, open(..., 'w'), open(..., 'wb').
"""

from pathlib import Path


class UnsupportedFormat(Exception):
    pass


class ExtractionError(Exception):
    pass


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
    """Primäre PDF-Extraktion via PyMuPDF — besser bei custom Fonts."""
    import fitz  # pymupdf
    result = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text().strip()
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
    """Gibt [{page_number, content}] zurück. PyMuPDF primär, pypdf als Fallback."""
    # PyMuPDF versuchen
    try:
        pages = _pdf_pages_pymupdf(path)
        if not _has_mojibake(pages):
            return pages
    except ImportError:
        pass  # pymupdf nicht installiert → pypdf versuchen
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc

    # Fallback: pypdf
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
    """PDF-seitenweises Splitting (Fallback für sehr lange Seiten)."""
    if len(text) <= max_len:
        return [{"page_number": page_number, "content": text}]
    mid = len(text) // 2
    split_at = text.rfind(" ", max(0, mid - 200), min(len(text), mid + 200))
    if split_at == -1:
        split_at = mid
    return [
        {"page_number": page_number, "content": text[:split_at].strip()},
        {"page_number": page_number, "content": text[split_at:].strip()},
    ]


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
                    "content":     c["content"],
                })
                idx += 1
        return chunks
    else:
        text, _ = extract(path)
        if not text:
            return []
        parts = split_text_into_chunks(text)
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

def extract_xlsx(path: Path) -> tuple[str, str]:
    try:
        import openpyxl
    except ImportError:
        raise UnsupportedFormat("openpyxl not installed")
    try:
        # READ-ONLY: NAS darf nie verändert werden — read_only=True
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=True):
                line = "  ".join(str(c) for c in row if c is not None)
                if line:
                    parts.append(line)
        wb.close()
        return "\n".join(parts), ""
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


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
