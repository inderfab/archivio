"""Test für einen echten Produktionsausfall (v3.5.0): `/static/*` lieferte
"Internal Server Error" statt logo.svg/htmx.min.js -- mit dem Effekt, dass ohne
htmx gar keine Suche und keine Foto-Galerie mehr funktionierten, obwohl /search bei
direktem Aufruf einwandfrei antwortete.

Ursache: `StaticFiles(directory="web/static")` in web/main.py stand mit einem
RELATIVEN Pfad da. Starlette löst den bei jeder Anfrage neu über os.getcwd() auf
(lookup_path() in starlette/staticfiles.py) statt ihn einmalig festzuhalten. Ein
.pkg-Update ersetzt den kompletten Resources-Ordner, BEVOR das Postinstall-Skript
die alte, noch laufende Instanz beendet -- deren Arbeitsverzeichnis zeigt für die
Dauer der Installation ins Leere, os.getcwd() wirft FileNotFoundError, und
lookup_path() fängt das nur um os.stat() herum ab, nicht um os.path.realpath().

Fix: absoluter Pfad über Path(__file__).resolve().parent (wie web/shared.py es bei
Jinja2Templates schon immer macht) -- braucht kein os.getcwd() mehr."""
import os


def test_static_files_survive_a_changed_working_directory(tmp_db, tmp_path):
    """Simuliert genau das Update-Szenario: das Arbeitsverzeichnis des Prozesses
    wechselt (im echten Fall, weil der alte Ordner unter ihm ersetzt wird) --
    /static/* darf davon unabhängig weiter funktionieren."""
    from fastapi.testclient import TestClient

    from web.main import app

    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)  # simuliert: relativer "web/static"-Pfad zeigt ins Leere
        c = TestClient(app)

        r = c.get("/static/logo.svg")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/svg+xml"

        r = c.get("/static/htmx.min.js")
        assert r.status_code == 200
    finally:
        os.chdir(original_cwd)


def test_static_mount_uses_an_absolute_path():
    """Direkte Absicherung gegen ein Zurückrutschen auf den relativen Pfad --
    os.path.realpath() braucht os.getcwd() nur für relative Eingaben."""
    from starlette.routing import Mount

    from web.main import app

    static_mount = next(r for r in app.routes if isinstance(r, Mount) and r.path == "/static")
    directory = static_mount.app.all_directories[0]
    assert os.path.isabs(directory), f"StaticFiles-Verzeichnis ist relativ: {directory!r}"
