"""
The profile record and the pure logic that resolves it.

No I/O, no language model, no clock of its own. The worker imports this, and
this imports nothing that could reinterpret the user's original sentence.

One window a day: up at a time, back down at a time (DESIGN.md section 5).

DESIGN.md sections 3, 5, 7.
"""

from __future__ import annotations

import datetime as _dt
import re
from zoneinfo import ZoneInfo

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
WEEKEND = ("Sat", "Sun")
MINUTES_PER_DAY = 24 * 60

# Tiers that cannot be scheduled at all, and why (DESIGN.md 1.1).
UNSCHEDULABLE_TIERS = {
    "developer": "the Developer tier cannot add units, has no SLA, and scaling "
                 "it causes downtime",
    "consumption": "the Consumption tier scales itself on traffic and cannot be "
                   "manually scaled",
}

# Maximum units per tier (DESIGN.md 1.1). None = no documented fixed limit.
TIER_MAX_UNITS = {
    "developer": 1,
    "basic": 2,
    "standard": 4,
    "premium": None,
    "basicv2": 10,
    "standardv2": 10,
    "premiumv2": 30,
}

DEFAULT_PREWARM_MINUTES = 45          # DESIGN.md D1
DEFAULT_SCALE_SECONDS = 45 * 60       # until measured (D1)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ProfileError(ValueError):
    """The profile is structurally invalid, or asks for something this tier
    cannot do. Raised on load and on save, so a bad record never reaches the
    worker."""


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def normalise_tier(name):
    """'Standard v2' / 'StandardV2' / 'standard_v2' -> 'standardv2'."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def parse_hhmm(value, field="time"):
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
            hit = [d for d in DAY_NAMES if d.lower() == key[:3]]
            if not hit:
                raise ProfileError("unknown day %r" % (day,))
            out.extend(hit)
    seen = []
    for day in out:
        if day not in seen:
            seen.append(day)
    return seen


def _require_int(container, key, where, minimum=0):
    if key not in container:
        raise ProfileError("%s is missing required field '%s'" % (where, key))
    value = container[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileError("%s.%s must be an integer, got %r" % (where, key, value))
    if value < minimum:
        raise ProfileError("%s.%s must be >= %d" % (where, key, minimum))
    return value


# --------------------------------------------------------------------------
# tier / zone rules  (DESIGN.md 1.1, 3.5)
# --------------------------------------------------------------------------

def check_tier_schedulable(tier):
    """Raise if this tier cannot be scheduled at all."""
    key = normalise_tier(tier)
    if key in UNSCHEDULABLE_TIERS:
        raise ProfileError("cannot schedule %s: %s" % (tier, UNSCHEDULABLE_TIERS[key]))
    if key not in TIER_MAX_UNITS:
        raise ProfileError("unknown APIM tier %r" % (tier,))
    return key


def tier_max_units(tier):
    return TIER_MAX_UNITS.get(normalise_tier(tier))


def check_units_for_tier(units, tier):
    """Refuse a unit count the tier cannot reach. Not a clamp -- the request is
    impossible, and clamping it would silently give someone less than they asked
    for."""
    maximum = tier_max_units(tier)
    if maximum is not None and units > maximum:
        raise ProfileError(
            "%s units exceeds the maximum of %d for the %s tier"
            % (units, maximum, tier))
    if units < 1:
        raise ProfileError("units must be at least 1")
    return units


def check_units_for_zones(units, zone_count):
    """A zone-pinned instance requires a multiple of the zone count."""
    if not zone_count or zone_count < 2:
        return units
    if units % zone_count:
        lower = (units // zone_count) * zone_count
        upper = lower + zone_count
        options = "%d or %d" % (lower, upper) if lower >= zone_count else str(upper)
        raise ProfileError(
            "%s units is not a multiple of the %d configured availability zones; "
            "use %s" % (units, zone_count, options))
    return units


# Measured, not from the docs. A BasicV2 instance scaled 1->2 in under one
# 60-second tick; the docs' "15-45 minutes" describes classic tiers and large or
# multi-region deployments. A flat 90-minute floor would refuse windows that are
# perfectly coherent on v2.
TIER_SCALE_SECONDS = {
    "basicv2": 5 * 60,
    "standardv2": 5 * 60,
    "premiumv2": 10 * 60,
}


def expected_scale_seconds(tier=None, measured=None):
    """Best estimate of how long a scale takes, most specific source first."""
    if measured:
        return int(measured)
    return TIER_SCALE_SECONDS.get(normalise_tier(tier), DEFAULT_SCALE_SECONDS)


def min_window_minutes(scale_seconds=None, tier=None):
    """A window must fit a scale up AND a scale down (DESIGN.md 3.1)."""
    return int(round(expected_scale_seconds(tier, scale_seconds) * 2 / 60.0))


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(profile):
    if not isinstance(profile, dict):
        raise ProfileError("profile must be an object")

    if not isinstance(profile.get("name"), str) or not profile["name"].strip():
        raise ProfileError("profile needs a non-empty name")

    target = profile.get("target")
    if not isinstance(target, dict):
        raise ProfileError("profile needs a target object")
    for field in ("subscription", "resourceGroup", "service"):
        if not isinstance(target.get(field), str) or not target[field].strip():
            raise ProfileError("target.%s is required" % field)

    tz = profile.get("timezone")
    if not isinstance(tz, str) or not tz:
        raise ProfileError("timezone is required (IANA name)")
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ProfileError("timezone %r is not a usable IANA zone: %s" % (tz, exc))

    profile["days"] = expand_days(profile.get("days"))

    up = parse_hhmm(profile.get("scaleUpAt"), "scaleUpAt")
    down = parse_hhmm(profile.get("scaleDownAt"), "scaleDownAt")
    if down <= up:
        raise ProfileError(
            "scaleDownAt (%s) must be after scaleUpAt (%s); overnight windows "
            "are not supported" % (profile.get("scaleDownAt"), profile.get("scaleUpAt")))

    _require_int(profile, "units", "profile", minimum=1)
    _require_int(profile, "baselineUnits", "profile", minimum=1)
    _require_int(profile, "prewarmMinutes", "profile", minimum=0)

    # The window must be long enough to scale up, be useful, and scale down.
    needed = min_window_minutes(profile.get("expectedScaleSeconds"),
                                tier=profile.get("tier"))
    if (down - up) < needed:
        raise ProfileError(
            "the window is %d minutes but a scale takes about %d minutes each "
            "way, so it needs at least %d. Widen it or lower the expected scale "
            "duration once you have measured it."
            % (down - up, needed // 2, needed))

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


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def resolve(profile, now):
    """What should this instance be set to at `now`?

    Wall-clock comparison in the profile's own timezone -- boundaries are never
    materialised as UTC instants, which is what makes DST fall out correctly.
    """
    validate(profile)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ProfileError("`now` must be timezone-aware")

    tz = ZoneInfo(profile["timezone"])
    local = now.astimezone(tz)
    minutes = local.hour * 60 + local.minute
    day_name = DAY_NAMES[local.weekday()]

    if profile.get("paused"):
        return {"units": None, "active": False, "window": None,
                "reason": "profile is paused"}

    end_date = profile.get("endDate")
    if end_date and local.date() > _dt.date.fromisoformat(end_date):
        return {"units": None, "active": False, "window": None,
                "reason": "profile ended %s" % end_date}

    up = parse_hhmm(profile["scaleUpAt"])
    down = parse_hhmm(profile["scaleDownAt"])
    prewarm = profile.get("prewarmMinutes", DEFAULT_PREWARM_MINUTES)
    effective_up = up - prewarm      # may be negative: reaches into yesterday

    # `days` names the BUSINESS day -- the day scaleUpAt falls on. Pre-warm that
    # crosses midnight therefore belongs to the following day's window.
    for business_day in (local.date(), local.date() + _dt.timedelta(days=1)):
        if DAY_NAMES[business_day.weekday()] not in profile["days"]:
            continue
        offset = (local.date() - business_day).days * MINUTES_PER_DAY + minutes
        if effective_up <= offset < down:
            return {
                "units": profile["units"],
                "active": True,
                "window": "up",
                "reason": "%s %s-%s %s (prewarm %dm, issued from %s)"
                          % (DAY_NAMES[business_day.weekday()],
                             profile["scaleUpAt"], profile["scaleDownAt"],
                             profile["timezone"], prewarm, fmt_hhmm(effective_up)),
            }

    return {
        "units": profile["baselineUnits"],
        "active": True,
        "window": "baseline",
        "reason": "outside the window (%s %s %s)"
                  % (day_name, fmt_hhmm(minutes), profile["timezone"]),
    }


# --------------------------------------------------------------------------
# guardrails (DESIGN.md 7)
# --------------------------------------------------------------------------

def clamp(units, bounds, tier=None, zone_count=0):
    """Clamp into human-authored bounds, then hard-stop at the tier maximum.

    -> (units, was_clamped, note). The tier ceiling is applied last and cannot
    be exceeded by any configured bound.
    """
    low = bounds.get("absoluteMin", 1)
    high = bounds.get("absoluteMax")
    if high is None:
        raise ProfileError("target bounds must set absoluteMax")

    tier_cap = tier_max_units(tier) if tier else None
    if tier_cap is not None:
        high = min(high, tier_cap)
    low = max(1, low)

    result = max(low, min(high, units))
    conflict = None

    # Zone-pinned instances need a multiple of the zone count. Round DOWN only:
    # a guardrail that hands back MORE than was asked for is a cost bug, and
    # rounding up could also carry the value above absoluteMax.
    if zone_count and zone_count >= 2 and result % zone_count:
        rounded = (result // zone_count) * zone_count
        if rounded >= low and rounded >= 1:
            result = rounded
        else:
            # No valid multiple exists inside the bounds -- e.g. bounds 1..2 on a
            # 3-zone instance. Refuse to invent one; keep the in-range value and
            # say so, because silently rounding up is how a clamp becomes a bill.
            conflict = ("no multiple of %d fits within bounds %d..%d; this "
                        "instance cannot run at %d units"
                        % (zone_count, low, high, result))

    if result == units and not conflict:
        return result, False, None
    note = "clamped %d -> %d (bounds %d..%d%s%s)" % (
        units, result, low, high,
        ", %s tier max %s" % (tier, tier_cap) if tier_cap is not None else "",
        ", %d zones" % zone_count if zone_count and zone_count >= 2 else "")
    if conflict:
        note += "; " + conflict
    return result, True, note


def describe(profile, tier=None):
    """The plain-language report shown after saving."""
    target = profile["target"]
    days = profile["days"]
    if days == list(WEEKDAYS):
        label = "Mon-Fri"
    elif days == list(WEEKEND):
        label = "Sat-Sun"
    elif len(days) == 7:
        label = "Every day"
    else:
        label = ", ".join(days)

    prewarm = profile.get("prewarmMinutes", DEFAULT_PREWARM_MINUTES)
    issued = fmt_hhmm(parse_hhmm(profile["scaleUpAt"]) - prewarm)
    lines = [
        "Target:     %s%s" % (target["service"],
                              "  (%s, currently %s units)" % (tier, profile["baselineUnits"])
                              if tier else ""),
        "%-11s %d units from %s to %s %s"
        % (label + ":", profile["units"], profile["scaleUpAt"],
           profile["scaleDownAt"], profile["timezone"]),
        "Otherwise:  %d units  (its capacity when this profile was created)"
        % profile["baselineUnits"],
        "Pre-warm:   %d minutes -- the scale is issued at %s" % (prewarm, issued),
        "End date:   %s" % (profile["endDate"] or
                            "None - repeat until paused, changed, or deleted"),
    ]
    return "\n".join(lines)
