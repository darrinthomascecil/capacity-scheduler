"""
Absolute per-target bounds (DESIGN.md section 6).

Human-authored, in a file the interpreter never writes. This is the one thing
standing between a misparsed sentence and a 400-node cluster, so it is
deliberately kept out of reach of the language path.
"""

from __future__ import annotations

import json
import os

DEFAULT_PATH = os.environ.get(
    "AKSPROFILE_BOUNDS",
    os.path.join(os.path.expanduser("~"), ".aksprofile", "targets.json"))

# Used when a target has no entry. Deliberately tight: a target nobody has
# thought about should not be scalable to anything interesting.
FALLBACK = {"absoluteMin": 0, "absoluteMax": 3}


def key(target):
    return "%s/%s/%s" % (target.get("resourceGroup"), target.get("cluster"),
                         target.get("nodePool"))


def load(path=None):
    path = path or DEFAULT_PATH
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        return json.load(handle)


def for_target(target, table=None, path=None):
    """-> ({absoluteMin, absoluteMax}, is_default)"""
    table = table if table is not None else load(path)
    entry = table.get(key(target))
    if entry is None:
        return dict(FALLBACK), True
    bounds = dict(FALLBACK)
    bounds.update(entry)
    if bounds.get("absoluteMax") is None:
        raise ValueError("bounds for %s must set absoluteMax" % key(target))
    if bounds["absoluteMin"] > bounds["absoluteMax"]:
        raise ValueError("bounds for %s: absoluteMin exceeds absoluteMax" % key(target))
    return bounds, False


def scaffold(target, pool):
    """A starting entry seeded from the pool's current state, for a person to edit."""
    current = pool.get("count") or 1
    return {key(target): {
        "absoluteMin": pool.get("min") if pool.get("min") is not None else max(0, current),
        "absoluteMax": pool.get("max") if pool.get("max") is not None else max(1, current),
        "_review": "Seeded from current state. Set these deliberately.",
    }}
