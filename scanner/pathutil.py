"""Pfad-Hilfsfunktionen, gemeinsam genutzt von scanner/norms.py und
scanner/block_list.py -- beide brauchen dieselbe NFC-Normalisierung
(macOS/SMB liefert NFD-zerlegte Umlaute) und Präfix-Logik auf Pfadkomponenten
statt auf Zeichenketten."""
from __future__ import annotations

import os
import unicodedata


def norm_path(p: str) -> str:
    return unicodedata.normalize("NFC", os.path.normpath(p))


def is_under(path: str, root: str) -> bool:
    """Präfix-Match auf Pfadkomponenten, nicht auf Strings.
    Verhindert, dass '/x/Normen2' gegen '/x/Normen' matcht."""
    path, root = norm_path(path), norm_path(root)
    return path == root or path.startswith(root + os.sep)
