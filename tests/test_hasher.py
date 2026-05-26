from pathlib import Path
from scanner.hasher import sha256


def test_sha256_stable(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello archivio")
    assert sha256(f) == sha256(f)


def test_sha256_differs(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    assert sha256(a) != sha256(b)


def test_sha256_known_value(tmp_path):
    import hashlib
    data = b"archivio"
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    assert sha256(f) == hashlib.sha256(data).hexdigest()
