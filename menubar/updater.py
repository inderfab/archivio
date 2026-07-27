"""Automatische Updates für Archivio Server. Prüft GitHub Releases, lädt das
signierte .pkg herunter, verifiziert Signatur + Team-ID und öffnet den
System-Installer. Keine eigene Kryptografie -- Sicherheitsanker ist
ausschließlich Apples Developer-ID-Signaturkette."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests
from packaging.version import Version

GITHUB_REPO      = "inderfab/archivio"
ASSET_SUFFIX     = ".pkg"
ASSET_NAME_HINT  = "archivio-server"
EXPECTED_TEAM_ID = "2USYCLVGTM"

_DOWNLOAD_DIR = Path.home() / "Library" / "Caches" / "Archivio" / "updates"


@dataclass
class UpdateInfo:
    version: str
    download_url: str
    asset_name: str


def _log_info(log, msg, *args):
    if log:
        log.info(msg, *args)


def _log_warning(log, msg, *args):
    if log:
        log.warning(msg, *args)


def pruefe_update(current_version: str, log=None) -> UpdateInfo | None:
    """Prüft GitHub Releases auf eine neuere Version mit passendem .pkg-Asset.
    Gibt bei jedem Fehler (Netzwerk, Parsing, kein passendes Asset, nicht
    neuer) None zurück -- die Prüfung darf den Nutzer nie stören."""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code != 200:
            return None
        data       = resp.json()
        remote_ver = data.get("tag_name", "").lstrip("v")
        if not remote_ver:
            return None
        if not (Version(remote_ver) > Version(current_version)):
            return None
        for asset in data.get("assets", []):
            name = asset.get("name", "")
            if ASSET_NAME_HINT in name and name.endswith(ASSET_SUFFIX):
                return UpdateInfo(
                    version=remote_ver,
                    download_url=asset["browser_download_url"],
                    asset_name=name,
                )
        return None
    except Exception as e:
        _log_warning(log, "Update-Check fehlgeschlagen: %s", e)
        return None


def _verify_pkg(pkg: Path, log=None) -> bool:
    """Verifiziert ein heruntergeladenes .pkg per pkgutil (Signatur + Team-ID)
    und spctl (Gatekeeper-Installations-Check)."""
    try:
        r = subprocess.run(
            ["pkgutil", "--check-signature", str(pkg)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            _log_warning(log, "pkgutil --check-signature fehlgeschlagen: %s", r.stdout)
            return False
        if "Developer ID Installer" not in r.stdout:
            _log_warning(log, "Kein Developer-ID-Installer-Signer: %s", r.stdout)
            return False
        if f"({EXPECTED_TEAM_ID})" not in r.stdout:
            _log_warning(log, "Unerwartete Team-ID: %s", r.stdout)
            return False
    except Exception as e:
        _log_warning(log, "pkgutil-Aufruf fehlgeschlagen: %s", e)
        return False

    try:
        r = subprocess.run(
            ["spctl", "-a", "-t", "install", str(pkg)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            _log_warning(log, "spctl lehnt Paket ab: %s", r.stderr)
            return False
    except Exception as e:
        _log_warning(log, "spctl-Aufruf fehlgeschlagen: %s", e)
        return False

    return True


def lade_und_pruefe(info: UpdateInfo, log=None) -> Path | None:
    """Lädt das Update-Paket herunter und verifiziert Signatur + Team-ID.
    Gibt den Pfad zum verifizierten .pkg zurück, oder None (Datei wird bei
    jeder fehlgeschlagenen Prüfung gelöscht)."""
    _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = _DOWNLOAD_DIR / info.asset_name
    try:
        with requests.get(info.download_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
    except Exception as e:
        _log_warning(log, "Update-Download fehlgeschlagen: %s", e)
        dest.unlink(missing_ok=True)
        return None

    if not _verify_pkg(dest, log):
        dest.unlink(missing_ok=True)
        return None

    _log_info(log, "Update %s heruntergeladen und verifiziert: %s", info.version, dest)
    return dest


def installiere(pkg: Path) -> None:
    """Öffnet den System-Installer für das .pkg -- kein stiller Root-Install."""
    subprocess.run(["open", str(pkg)])
