import hashlib
from pathlib import Path

CHUNK = 1 << 20  # 1 MB


def sha256(path: Path) -> str:
    # READ-ONLY: NAS darf nie verändert werden
    h = hashlib.sha256()
    with open(path, "rb") as f:  # READ-ONLY: ausschliesslich lesender Zugriff
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()
