from pathlib import Path
import shutil
import yaml

_CONFIG_PATH  = Path(__file__).parent.parent / "config.yaml"
_EXAMPLE_PATH = Path(__file__).parent.parent / "config.yaml.example"
_settings: dict = {}


def _load() -> dict:
    if not _CONFIG_PATH.exists():
        if _EXAMPLE_PATH.exists():
            shutil.copy(_EXAMPLE_PATH, _CONFIG_PATH)
        else:
            _CONFIG_PATH.write_text("{}\n", encoding="utf-8")
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


def load_all() -> dict:
    return _load()


def save(updates: dict):
    existing = _load()
    _deep_update(existing, updates)
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(existing, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    reload()


def _deep_update(base: dict, updates: dict):
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def reload():
    global _settings
    _settings = _load()
