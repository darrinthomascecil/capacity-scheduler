"""
The profile record and the pure logic that resolves it.

Nothing in this module performs I/O, calls a language model, or reads a clock.
`resolve` takes an instant and returns what the pool should be set to. That is
what makes acceptance criterion 4 structurally true rather than merely tested:
the worker imports this, and this imports nothing that could reinterpret the
user's original sentence.

Spec: DESIGN.md sections 3, 5, 6, 7.
"""

from __future__ import annotations

import datetime as _dt
import re
from zoneinfo import ZoneInfo

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
WEEKEND = ("Sat", "Sun")

MODE_COUNT = "count"      # "four nodes"      -> exact node count
MODE_MINIMUM = "minimum"  # "minimum four"    -> autoscaler floor
MODES = (MODE_COUNT, MODE_MINIMUM)

MINUTES_PER_DAY = 24 * 60

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ProfileError(ValueError):
    """A profile is structurally invalid. Raised on load and on save, so a bad
    record can never reach the worker."""


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

def parse_hhmm(value, field="time"):
    """'06:00' -> 360. Accepts '24:00' as end-of-day."""
    if not isinstance(value, str):
        raise ProfileError("%s must be a 'HH:MM' string, got %r" % (field, value))
    if value == "24:00":
        return MINUTES_PER_DAY
    match = _TIME_RE.match(value)
    if not match:
        raise ProfileError("%s must be 24-hour 'HH:MM', got %r" % (field, value))
    return int(match.group(1)) * 60 + int(match.group(2))


def fmt_hhmm(minutes):
    m = minutes % MINUTES_PER_DAY
    return "%02d:%02d" % (m // 60, m % 60)


def expand_days(days):
    """Accepts day names plus the shorthands the interpreter may emit."""
    if not isinstance(days, (list, tuple)) or not days:
        raise ProfileError("days must be a non-empty list")
    out = []
    for day in days:
        if not isinstance(day, str):
            raise ProfileError("day must be a string, got %r" % (day,))
        key = day.strip().lower()
        if key in ("weekday", "weekdays"):
            out.extend(WEEKDAYS)
        elif key in ("weekend", "weekends"):
            out.extend(WEEKEND)
        elif key in ("daily", "everyday", "every day", "all"):
            out.extend(DAY_NAMES)
        else:
            match = [d for d in DAY_NAMES if d.lower() == key[:3]]
            if not match:
                raise ProfileError("unknown day %r" % (day,))
            out.extend(match)
    seen = []
    for day in out:
        if day not in seen:
            seen.append(day)
    return seen


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(profile):
    """Raise ProfileError on the first problem. Returns the profile."""
    if not isinstance(profile, dict):
        raise ProfileError("profile must be an object")

    name = profile.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("profile needs a non-empty name")

    target = profile.get("target")
    if not isinstance(target, dict):
        raise ProfileError("profile needs a target object")
    for field in ("subscription", "resourceGroup", "cluster", "nodePool"):
        if not isinstance(target.get(field), str) or not target[field].strip():
            raise ProfileError("target.%s is required" % field)

    tz = profile.get("timezone")
    if not isinstance(tz, str) or not tz:
        raise ProfileError("timezone is required (IANA name)")
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ProfileError("timezone %r is not a usable IANA zone: %s" % (tz, exc))

    mode = profile.get("mode")
    if mode not in MODES:
        raise ProfileError("mode must be one of %s, got %r" % (", ".join(MODES), mode))

    windows = profile.get("windows")
    if not isinstance(windows, list) or not windows:
        raise ProfileError("profile needs at least one window")
    for index, window in enumerate(windows):
        _validate_window(window, index)

    otherwise = profile.get("otherwise")
    if isinstance(otherwise, bool) or not isinstance(otherwise, int):
        raise ProfileError("'otherwise' must be an integer node value")
    if otherwise < 0:
        raise ProfileError("'otherwise' must be >= 0")

    end_date = profile.get("endDate")
    if end_date is not None:
        if not isinstance(end_date, str):
            raise ProfileError("endDate must be null or 'YYYY-MM-DD'")
        try:
            _dt.date.fromisoformat(end_date)
        except ValueError:
            raise ProfileError("endDate must be 'YYYY-MM-DD', got %r" % (end_date,))

    if not isinstance(profile.get("paused", False), bool):
        raise ProfileError("paused must be a boolean")

    return profile


def _validate_window(window, index):
    where = "window %d" % index
    if not isinstance(window, dict):
        raise ProfileError("%s must be an object" % where)

    window["days"] = expand_days(window.get("days"))

    start = parse_hhmm(window.get("start"), "%s start" % where)
    end = parse_hhmm(window.get("end"), "%s end" % where)
    if end <= start:
        raise ProfileError(
            "%s: end (%s) must be after start (%s). Overnight windows are not "
            "supported; split them into two windows."
            % (where, window.get("end"), window.get("start")))

    value = window.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError("%s value must be an integer" % where)
    if value < 0:
        raise ProfileError("%s value must be >= 0" % where)


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def resolve(profile, now):
    """What should this pool be set to at `now`?

    Returns {value, mode, window, reason, active}. Window boundaries are
    compared as wall-clock times in the profile's own timezone -- never
    materialised as UTC instants -- which is what makes the DST behaviour in
    DESIGN.md section 5 fall out naturally.
    """
    validate(profile)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ProfileError("`now` must be timezone-aware")

    tz = ZoneInfo(profile["timezone"])
    local = now.astimezone(tz)
    minutes = local.hour * 60 + local.minute
    day_name = DAY_NAMES[local.weekday()]

    if profile.get("paused"):
        return {"value": None, "mode": profile["mode"], "window": None,
                "active": False, "reason": "profile is paused"}

    end_date = profile.get("endDate")
    if end_date and local.date() > _dt.date.fromisoformat(end_date):
        return {"value": None, "mode": profile["mode"], "window": None,
                "active": False, "reason": "profile ended %s" % end_date}

    # First match wins, in array order (DESIGN.md section 7).
    for index, window in enumerate(profile["windows"]):
        if day_name not in window["days"]:
            continue
        start = parse_hhmm(window["start"])
        end = parse_hhmm(window["end"])
        if start <= minutes < end:
            return {
                "value": window["value"],
                "mode": profile["mode"],
                "window": window.get("label") or "window %d" % index,
                "active": True,
                "reason": "%s %s-%s %s" % (day_name, window["start"], window["end"],
                                           profile["timezone"]),
            }

    return {
        "value": profile["otherwise"],
        "mode": profile["mode"],
        "window": None,
        "active": True,
        "reason": "outside every window (%s %s %s)"
                  % (day_name, fmt_hhmm(minutes), profile["timezone"]),
    }


# --------------------------------------------------------------------------
# guardrails (DESIGN.md section 6)
# --------------------------------------------------------------------------

def clamp(value, bounds, is_system_pool=False):
    """Clamp a resolved value into human-authored bounds.

    -> (value, was_clamped, note). The clamp is the reason a misparse is
    survivable: the worst outcome is a valid node count, never zero and never
    unbounded.
    """
    low = bounds.get("absoluteMin", 0)
    high = bounds.get("absoluteMax")
    if high is None:
        raise ProfileError("target bounds must set absoluteMax")
    if is_system_pool:
        low = max(low, 1)  # F20 -- a system pool may not go to zero

    clamped = max(low, min(high, value))
    if clamped == value:
        return clamped, False, None
    return clamped, True, "clamped %d -> %d (bounds %d..%d%s)" % (
        value, clamped, low, high, ", system pool" if is_system_pool else "")


def describe(profile):
    """The plain-language report shown after saving (F10)."""
    target = profile["target"]
    unit = "nodes" if profile["mode"] == MODE_COUNT else "minimum nodes"
    lines = ["Target: %s / %s" % (target["cluster"], target["nodePool"])]
    for window in profile["windows"]:
        days = window["days"]
        if days == list(WEEKDAYS):
            label = "Monday-Friday"
        elif days == list(WEEKEND):
            label = "Saturday-Sunday"
        elif len(days) == 7:
            label = "Every day"
        else:
            label = ", ".join(days)
        lines.append("%s, %s-%s: %d %s"
                     % (label, window["start"], window["end"], window["value"], unit))
    lines.append("Otherwise: %d %s" % (profile["otherwise"], unit))
    lines.append("End date: %s" % (profile["endDate"] or
                                   "None - repeat until paused, changed, or deleted"))
    lines.append("Timezone: %s" % profile["timezone"])
    return "\n".join(lines)
