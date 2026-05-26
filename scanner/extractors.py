"""
Extraktoren pro Dateityp. Jeder Extraktor nimmt einen Path und gibt (text, language) zurück.
Bei nicht unterstützten Typen: raise UnsupportedFormat.
Bei Fehler in der Extraktion: raise ExtractionError.
"""

from pathlib import Path


class UnsupportedFormat(Exception):
    pass


class ExtractionError(Exception):
    pass


def extract_txt(path: Path) -> tuple[str, str]:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return path.read_text(encoding=enc), ""
        except UnicodeDecodeError:
            continue
    raise ExtractionError(f"Cannot decode {path}")


def extract_pdf(path: Path) -> tuple[str, str]:
    try:
        import pypdf  # optional dependency
    except ImportError:
        raise UnsupportedFormat("pypdf not installed")
    try:
        reader = pypdf.PdfReader(str(path))
        parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                parts.append(text)
        return "\n".join(parts), ""
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


def extract_docx(path: Path) -> tuple[str, str]:
    try:
        import docx  # python-docx, optional
    except ImportError:
        raise UnsupportedFormat("python-docx not installed")
    try:
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text), ""
    except Exception as exc:
        raise ExtractionError(str(exc)) from exc


_REGISTRY: dict[str, callable] = {
    ".txt": extract_txt,
    ".md":  extract_txt,
    ".pdf": extract_pdf,
    ".docx": extract_docx,
}


def extract(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    fn = _REGISTRY.get(ext)
    if fn is None:
        raise UnsupportedFormat(f"No extractor for {ext}")
    return fn(path)
