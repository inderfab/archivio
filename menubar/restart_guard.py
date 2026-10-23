"""Bremse und Diagnose fuer die Server-Neustarts des Watchdogs (server_app.py).

Ohne Bremse startet der Watchdog bei einem Server, der sofort wieder stirbt (z.B. weil
ein anderer Prozess Port 8000 haelt), alle ~20 s neu -- endlos, mit einer Benachrichtigung
pro Versuch und ohne je zu sagen warum."""
from __future__ import annotations

import errno
import socket
import subprocess
import time
from pathlib import Path

# Wartezeit bis zum naechsten Versuch nach dem n-ten Fehlschlag (Sekunden).
BACKOFF = {1: 0, 2: 60, 3: 120, 4: 300}
MAX_FAILS = 5            # danach wird nicht mehr automatisch neu gestartet
STABLE_AFTER = 60        # so lange muss der Server laufen, damit der Zaehler zurueckgesetzt wird


class RestartGuard:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.fails = 0
        self.next_attempt_at = 0.0
        self.gave_up = False
        self._healthy_since: float | None = None

    def may_restart(self) -> bool:
        return not self.gave_up and self._clock() >= self.next_attempt_at

    def failed(self) -> None:
        """Nach jedem automatischen Neustartversuch aufrufen."""
        self._healthy_since = None
        self.fails += 1
        if self.fails >= MAX_FAILS:
            self.gave_up = True
            return
        self.next_attempt_at = self._clock() + BACKOFF.get(self.fails, BACKOFF[4])

    def healthy(self) -> None:
        """Bei jedem Durchlauf aufrufen, in dem der Server laeuft UND antwortet."""
        now = self._clock()
        if self._healthy_since is None:
            self._healthy_since = now
        elif now - self._healthy_since >= STABLE_AFTER:
            self.reset()

    def reset(self) -> None:
        self.fails = 0
        self.next_attempt_at = 0.0
        self.gave_up = False
        self._healthy_since = None


def port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError as e:
        return e.errno == errno.EADDRINUSE
    finally:
        s.close()


def port_holders(port: int) -> list[str]:
    """Lesbare Liste der Prozesse, die `port` belegen. Leer, wenn lsof nichts zeigt --
    typisch bei Prozessen eines anderen Benutzers."""
    try:
        r = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            out.append(f"{parts[0]} (PID {parts[1]}, Benutzer {parts[2]})")
    return out


def log_tail(path: Path, lines: int = 8) -> list[str]:
    try:
        return [l.rstrip() for l in path.read_text(errors="replace").splitlines()[-lines:] if l.strip()]
    except Exception:
        return []


def diagnose(port: int, server_log: Path) -> tuple[str, str]:
    """(kurz, lang): kurz fuer Menue/Dialog, lang fuers Log."""
    tail = log_tail(server_log)
    if port_in_use(port):
        holders = port_holders(port)
        if holders:
            short = f"Port {port} ist belegt von {', '.join(holders)}."
        else:
            short = (f"Port {port} ist belegt, der Verursacher ist nicht sichtbar "
                     f"(vermutlich ein Archivio-Prozess eines anderen Benutzers).")
    elif tail:
        short = f"Der Server beendet sich beim Start: {tail[-1][:160]}"
    else:
        short = "Der Server beendet sich beim Start, ein Grund ist nicht im Log."
    long = short + ("\nLetzte Zeilen aus server.log:\n  " + "\n  ".join(tail) if tail else "")
    return short, long
