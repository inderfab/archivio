import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "menubar"))
import restart_guard as rg  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_backoff_and_give_up():
    c = Clock()
    g = rg.RestartGuard(clock=c)
    assert g.may_restart()
    g.failed()                       # 1. Fehlschlag: naechster Versuch sofort moeglich
    assert g.may_restart()
    g.failed()                       # 2.: 60 s Pause
    assert not g.may_restart()
    c.t += 61
    assert g.may_restart()
    g.failed(); c.t += 121          # 3.: 120 s
    assert g.may_restart()
    g.failed(); c.t += 301          # 4.: 300 s
    assert g.may_restart()
    g.failed()                       # 5.: aufgegeben
    assert g.gave_up and not g.may_restart()
    c.t += 10_000
    assert not g.may_restart(), "nach dem Aufgeben nie wieder automatisch"
    g.reset()
    assert g.may_restart() and g.fails == 0


def test_stable_run_resets_counter():
    c = Clock()
    g = rg.RestartGuard(clock=c)
    g.failed(); g.failed()
    g.healthy()
    c.t += rg.STABLE_AFTER - 1
    g.healthy()
    assert g.fails == 2
    c.t += 2
    g.healthy()
    assert g.fails == 0


def test_failure_interrupts_stability_window():
    c = Clock()
    g = rg.RestartGuard(clock=c)
    g.failed()
    g.healthy()
    c.t += 30
    g.failed()                       # stirbt erneut bevor 60 s um sind
    c.t += 100
    g.healthy()
    assert g.fails == 2


def test_port_in_use_and_diagnosis(tmp_path):
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert rg.port_in_use(port) is True
        short, long = rg.diagnose(port, tmp_path / "nix.log")
        assert f"Port {port} ist belegt" in short
        assert "PID" in short, "der eigene Prozess haelt den Port und muss genannt werden"
    finally:
        s.close()
    assert rg.port_in_use(port) is False


def test_diagnose_reports_server_log_when_port_free(tmp_path):
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    log = tmp_path / "server.log"
    log.write_text("start\nImportError: No module named foo\n")
    short, long = rg.diagnose(port, log)
    assert "ImportError: No module named foo" in short
    assert "letzte zeilen" in long.lower()
