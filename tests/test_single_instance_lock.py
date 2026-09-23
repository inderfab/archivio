"""Tests für die Einmal-Start-Sperre (shared/menubar_bridge.py).

Hintergrund: der Server konnte über zwei Wege gleichzeitig starten -- den
LaunchAgent io.archivio.server und ein Anmeldeobjekt aus älteren Installationen.
Beide Supervisor starteten je einen uvicorn, und jeder räumte über
_kill_port_8000() den anderen ab. Auf dem Produktivrechner war das als zwei
uvicorn-Starts im Abstand von 37 ms plus ein Watchdog-Neustart 15 s später im Log
sichtbar. Die Sperre ist die eigentliche Absicherung dagegen -- unabhängig davon,
wie viele Autostart-Einträge sich über die Jahre angesammelt haben."""
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "shared"))
import menubar_bridge as bridge  # noqa: E402

_SHARED = str(Path(__file__).parent.parent / "shared")


def _child(name: str, hold: bool) -> subprocess.CompletedProcess:
    """Startet einen eigenen Prozess, der die Sperre zu nehmen versucht.

    Muss ein echter Prozess sein: flock ist pro Prozess, im selben Prozess wäre
    ein zweiter Versuch immer erfolgreich und der Test wertlos."""
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {_SHARED!r})
        import menubar_bridge as bridge
        ok = bridge.acquire_single_instance_lock({name!r})
        print("OK" if ok else "BELEGT", flush=True)
        if {hold!r} and ok:
            time.sleep(30)
    """)
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, text=True)


def test_second_instance_is_refused(tmp_path, monkeypatch):
    name = "archivio-test-lock"
    erster = _child(name, hold=True)
    try:
        assert erster.stdout.readline().strip() == "OK"

        zweiter = _child(name, hold=False)
        assert zweiter.stdout.readline().strip() == "BELEGT"
        zweiter.wait(timeout=10)
    finally:
        erster.kill()
        erster.wait(timeout=10)


def test_lock_is_released_when_process_ends():
    """Nach einem Absturz darf die Sperre nicht hängenbleiben -- sonst startet der
    Server nach einem harten Neustart des Macs nie wieder."""
    name = "archivio-test-lock-release"
    erster = _child(name, hold=True)
    assert erster.stdout.readline().strip() == "OK"
    erster.kill()
    erster.wait(timeout=10)

    zweiter = _child(name, hold=False)
    assert zweiter.stdout.readline().strip() == "OK"
    zweiter.wait(timeout=10)


def test_lock_succeeds_in_this_process():
    assert bridge.acquire_single_instance_lock("archivio-test-lock-inprocess") is True


def test_failed_attempts_do_not_leak_file_descriptors():
    """Die zweite Instanz beendet sich nicht, sondern wartet in Bereitschaft und
    versucht es alle 30 s erneut (KeepAlive=true im LaunchAgent darf nicht in eine
    Neustartschleife laufen). Bliebe pro Fehlversuch ein Handle offen, wäre nach
    rund einem Tag das Deskriptor-Limit erreicht."""
    import os

    name = "archivio-test-lock-fd"
    halter = _child(name, hold=True)
    try:
        assert halter.stdout.readline().strip() == "OK"

        vorher = len(os.listdir("/dev/fd"))
        for _ in range(200):
            assert bridge.acquire_single_instance_lock(name) is False
        assert len(os.listdir("/dev/fd")) <= vorher + 2
    finally:
        halter.kill()
        halter.wait(timeout=10)
