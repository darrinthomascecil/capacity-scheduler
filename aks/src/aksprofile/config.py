"""
Configuration, loaded from the environment with an optional .env file.

Containers get their environment from `--env-file` / compose, so .env parsing
matters mainly for local runs. Kept to the standard library -- a config loader
is not worth a dependency.

Precedence: real environment > .env file > default. An exported variable always
wins, so a shell override behaves the way people expect.
"""

from __future__ import annotations

import os

DEFAULTS = {
    "AKSPROFILE_STORE": None,                      # required, no default
    "AKSPROFILE_BOUNDS": os.path.join(os.path.expanduser("~"), ".aksprofile", "targets.json"),
    "AKSPROFILE_TICK_SECONDS": "60",
    "AKSPROFILE_DRY_RUN": "false",
    "AKSPROFILE_HEARTBEAT": "/tmp/aksprofile.heartbeat",
    "AKSPROFILE_MODEL": "gpt-5.4-mini",
    "AKSPROFILE_BASE_URL": "https://api.openai.com/v1",
    "AKSPROFILE_API_KEY_VAR": "OPENAI_API_KEY",
}

_LOADED = False


def load_dotenv(path=None, override=False):
    """Read KEY=VALUE lines into os.environ. Returns the keys it set."""
    global _LOADED
    path = path or os.environ.get("AKSPROFILE_ENV_FILE") or ".env"
    applied = []
    if not os.path.exists(path):
        _LOADED = True
        return applied
    with open(path) as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
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


def require(name):
    value = get(name)
    if not value:
        raise ValueError("%s is required. Set it in the environment or .env "
                         "(see .env.example)." % name)
    return value
