"""Configuration from the environment, with an optional .env file."""

from __future__ import annotations

import os

DEFAULTS = {
    "APIMPROFILE_STORE": None,                       # required, no default
    "APIMPROFILE_BOUNDS": os.path.join(os.path.expanduser("~"), ".apimprofile", "targets.json"),
    "APIMPROFILE_TICK_SECONDS": "300",               # DESIGN.md 3.3
    "APIMPROFILE_DRY_RUN": "false",
    "APIMPROFILE_HEARTBEAT": "/tmp/apimprofile.heartbeat",
    "APIMPROFILE_MODEL": "gpt-5.4-mini",
    "APIMPROFILE_BASE_URL": "https://api.openai.com/v1",
    "APIMPROFILE_API_KEY_VAR": "OPENAI_API_KEY",
}

_LOADED = False


def load_dotenv(path=None, override=False):
    global _LOADED
    path = path or os.environ.get("APIMPROFILE_ENV_FILE") or ".env"
    applied = []
    if os.path.exists(path):
        with open(path) as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if override or key not in os.environ:
                    os.environ[key] = value
                    applied.append(key)
    _LOADED = True
    return applied


def get(name, default=None):
    if not _LOADED:
        load_dotenv()
    return os.environ.get(name, DEFAULTS.get(name, default))


def get_bool(name):
    return str(get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def get_int(name):
    try:
        return int(get(name))
    except (TypeError, ValueError):
        return int(DEFAULTS.get(name, 0))
