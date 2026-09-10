"""
Absolute per-instance unit bounds (DESIGN.md 7).

Human-authored, in a file the interpreter never writes. The tier maximum is
applied on top of these and cannot be exceeded by any value set here.
"""

from __future__ import annotations

import json
import os

from . import config

# A target nobody has thought about should not be scalable to anything
# interesting.
FALLBACK = {"absoluteMin": 1, "absoluteMax": 2}


def key(target):
    return "%s/%s" % (target.get("resourceGroup"), target.get("service"))


def load(path=None):
    path = path or config.get("APIMPROFILE_BOUNDS")
    if not path or not os.path.exists(path):
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
    bounds.update({k: v for k, v in entry.items() if not k.startswith("_")})
    if bounds.get("absoluteMax") is None:
        raise ValueError("bounds for %s must set absoluteMax" % key(target))
    if bounds["absoluteMin"] > bounds["absoluteMax"]:
        raise ValueError("bounds for %s: absoluteMin exceeds absoluteMax" % key(target))
    return bounds, False


def scaffold(target, instance):
    """A starting entry seeded from current state, for a person to edit."""
    current = instance.get("capacity") or 1
    return {key(target): {
        "absoluteMin": max(1, current),
        "absoluteMax": max(1, current),
        "_review": "Seeded from current capacity. Set these deliberately -- they "
                   "are what stops a misread sentence becoming a large bill.",
        "_tier": instance.get("tier"),
    }}
