"""config.json + config.local.json loader.

config.json is committed to a public repository, so personal values (local
paths, account names, personal lists) live in config.local.json instead,
which is gitignored and deep-merged over config.json: nested objects merge
key by key, anything else (strings, lists) in the local file replaces the
config.json value. See config.local.example.json for the keys it holds.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

CONFIG_FILE = "config.json"
LOCAL_CONFIG_FILE = "config.local.json"


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        logging.warning("[local_config] %s is invalid JSON; ignored.", path.name)
        return {}
    return data if isinstance(data, dict) else {}


def _merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(root: Path) -> dict:
    """config.json with config.local.json merged over it ({} if neither exists)."""
    return _merge(_read(root / CONFIG_FILE), _read(root / LOCAL_CONFIG_FILE))
