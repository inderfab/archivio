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

def extract_pdf(path: Path) -> tuple[str, str]:
    try:
        import pypdf
    except ImportError:
        raise UnsupportedFormat("pypdf not installed")
    try:
        # READ-ONLY: NAS darf nie verändert werden — PdfReader öffnet nur lesend
        reader = pypdf.PdfReader(str(path))
        parts = [t for page in reader.pages if (t := page.extract_text())]
        return "\n".join(parts), ""
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


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
