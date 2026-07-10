import signal

from scanner.extractors import split_text_into_chunks


def _with_timeout(fn, seconds=10):
    """Führt fn mit hartem Timeout aus — verhindert, dass eine (regressierte)
    Endlosschleife die ganze Test-Suite aufhängt."""
    def _handler(signum, frame):
        raise TimeoutError("split_text_into_chunks hing (Endlosschleife?)")
    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_chunking_terminates_on_early_blank_line_then_body():
    """Regression: eine frühe Leerzeile (\\n\\n) gefolgt von langem Fliesstext ohne
    weitere Leerzeilen führte zur Endlosschleife (WinCan-Kamera-Log-Dateien) →
    Worker hing 120s → Projekt-Scan brach mit 'NAS prüfen' ab."""
    text = (
        "[ClipInfo]\nCreationDate=x\nApplication=WinCan\n\n[Indexing]\n"
        + "\n".join(f'Val{i:05d}="{i}.0;0.0;0"' for i in range(500))
    )
    parts = _with_timeout(lambda: split_text_into_chunks(text), seconds=10)
    assert len(parts) > 0
    # Der gesamte Text wird abgedeckt (kein Datenverlust bis zum Ende)
    assert any("Val00499" in p for p in parts)


def test_chunking_normal_text_unchanged():
    """Normaler Mehr-Absatz-Text wird weiterhin sinnvoll gechunkt."""
    text = "\n\n".join(f"Absatz {i}. " + "Wort " * 100 for i in range(10))
    parts = _with_timeout(lambda: split_text_into_chunks(text), seconds=10)
    assert len(parts) > 1
    assert all(len(p) > 20 for p in parts)
