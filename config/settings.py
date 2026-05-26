from pathlib import Path
import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
_settings: dict = {}


def _load() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get(key: str, default=None):
    global _settings
    if not _settings:
        _settings = _load()
    keys = key.split(".")
    val = _settings
    for k in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(k, default)
    return val


def reload():
    global _settings
    _settings = _load()
