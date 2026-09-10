"""
Natural language -> profile, ONCE, at creation (DESIGN.md 6).

Four things come from the prompt: service, units, up time, down time.
Nothing in the execution path imports this module.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from . import config
from .model import (
    DAY_NAMES, DEFAULT_PREWARM_MINUTES, ProfileError, check_tier_schedulable,
    check_units_for_tier, check_units_for_zones, min_window_minutes, parse_hhmm,
)

AZURE_AI_HOSTS = (".cognitiveservices.azure.com", ".openai.azure.com",
                  ".services.ai.azure.com")
AZURE_AI_SCOPE = "https://cognitiveservices.azure.com"


class InterpretError(RuntimeError):
    pass


class NeedsClarification(Exception):
    def __init__(self, questions):
        self.questions = questions
        super().__init__("; ".join(questions))


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service", "requestedService", "units", "scaleUpAt",
                 "scaleDownAt", "timezone", "days", "missing", "notes"],
    "properties": {
        "service": {"type": "string",
                    "description": "The APIM instance to scale. MUST be one of the "
                                   "names listed in the user message, or empty if "
                                   "the text names none or names one not listed."},
        "requestedService": {"type": "string",
                             "description": "The instance name the text asked for, "
                                            "VERBATIM, even if not in the list. Used "
                                            "to write a helpful error."},
        "units": {"type": "integer", "description": "Units while scaled UP."},
        "scaleUpAt": {"type": "string", "description": "24-hour HH:MM."},
        "scaleDownAt": {"type": "string",
                        "description": "24-hour HH:MM, after scaleUpAt."},
        "timezone": {"type": "string", "description": "IANA name, or empty."},
        "days": {"type": "array",
                 "items": {"type": "string", "enum": list(DAY_NAMES)},
                 "description": "Days the window applies. All seven if unstated."},
        "missing": {"type": "array",
                    "items": {"type": "string",
                              "enum": ["service", "units", "scale_up_time",
                                       "scale_down_time", "timezone"]},
                    "description": "Anything you could NOT determine. Never guess "
                                   "to avoid listing here."},
        "notes": {"type": "string", "description": "Assumptions you made."},
    },
}

SYSTEM_PROMPT = """\
You convert plain-English Azure API Management scaling schedules into one daily
window.

Rules:
- `scaleUpAt`/`scaleDownAt` are 24-hour HH:MM wall-clock times. `scaleDownAt`
  must be AFTER `scaleUpAt`. Overnight windows are not supported.
- `units` is the number of APIM scale units while scaled up. It is a whole
  number of units, not a percentage and not a request rate.
- `service` must come from the inventory in the user message. Never invent one.
  `requestedService` is what the text asked for verbatim, even if it is not in
  the inventory.
- If the text gives no scale-down time, put "scale_down_time" in `missing`. Do
  NOT invent one -- a schedule with no scale-down means paying for peak capacity
  overnight.
- "weekday" means Mon-Fri, "weekend" Sat-Sun. If days are unstated, use all
  seven.
- Do not subtract any warm-up from `scaleUpAt`. Give the time the user asked for.
- Use only IANA timezone names.
"""

QUESTIONS = {
    "service": "Which API Management instance? Name it in the prompt, or pass --service.",
    "units": "How many units exactly?",
    "scale_up_time": "What time should it scale up?",
    "scale_down_time": "What time should it scale back down?",
    "timezone": "Which timezone? (e.g. America/Chicago)",
}


def _is_azure_endpoint(url):
    return any(host in (url or "") for host in AZURE_AI_HOSTS)


def _azure_token():
    if shutil.which("az") is None:
        raise InterpretError("the endpoint is an Azure AI resource but the Azure "
                             "CLI is not available to get a token")
    try:
        token = subprocess.run(
            ["az", "account", "get-access-token", "--resource", AZURE_AI_SCOPE,
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise InterpretError("could not get an Azure AI token (try `az login`): %s" % exc)
    if not token:
        raise InterpretError("Azure returned an empty token -- run `az login`")
    return token


def resolve_credential():
    var = config.get("APIMPROFILE_API_KEY_VAR")
    explicit = os.environ.get(var)
    if explicit:
        return explicit
    if _is_azure_endpoint(config.get("APIMPROFILE_BASE_URL")):
        return _azure_token()
    raise InterpretError(
        "%s is not set. Either export it, or point APIMPROFILE_BASE_URL at an "
        "Azure AI endpoint to authenticate with your Azure identity." % var)


def describe_inventory(inventory):
    lines = []
    for row in sorted(inventory or [], key=lambda r: r["service"]):
        zones = row.get("zoneCount") or 0
        lines.append("  %s  (resource group %s, tier %s, currently %s units%s)"
                     % (row["service"], row["resourceGroup"], row.get("tier"),
                        row.get("capacity"),
                        ", %d availability zones" % zones if zones >= 2 else ""))
    return "\n".join(lines) or "  (none visible)"


def call_model(text, inventory=None, timeout=60):
    api_key = resolve_credential()
    payload = {
        "model": config.get("APIMPROFILE_MODEL"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "API Management instances available:\n%s\n\n"
                                        "Schedule:\n%s"
                                        % (describe_inventory(inventory), text)},
        ],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "apim_schedule", "strict": True,
                                            "schema": SCHEMA}},
    }
    request = urllib.request.Request(
        config.get("APIMPROFILE_BASE_URL").rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer %s" % api_key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise InterpretError("HTTP %d: %s"
                             % (exc.code, exc.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as exc:
        raise InterpretError("network: %s" % exc.reason)
    try:
        return json.loads(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError) as exc:
        raise InterpretError("could not read model output: %s" % exc)


def resolve_instance(proposal, inventory):
    """Match what the model named against instances that exist."""
    requested = (proposal.get("requestedService") or "").strip()
    if not requested:
        return None
    name = (proposal.get("service") or "").strip().lower()
    hits = [r for r in inventory if r["service"].lower() == name] if name else []
    if len(hits) == 1:
        return hits[0]
    raise InterpretError(
        "no API Management instance named %r. Available: %s"
        % (requested, ", ".join(sorted(r["service"] for r in inventory)) or "(none)"))


def to_profile(name, text, instance, proposal, timezone=None, prewarm=None):
    """Build the record, or raise. Every refusal below is enforced HERE, in
    ordinary code -- not left to the model's judgement (DESIGN.md 6.2)."""
    missing = [m for m in (proposal.get("missing") or []) if m != "service"]

    tz = timezone or (proposal.get("timezone") or "").strip()
    if not tz and "timezone" not in missing:
        missing.append("timezone")
    if not proposal.get("units") and "units" not in missing:
        missing.append("units")
    if not proposal.get("scaleUpAt") and "scale_up_time" not in missing:
        missing.append("scale_up_time")
    if not proposal.get("scaleDownAt") and "scale_down_time" not in missing:
        missing.append("scale_down_time")

    if missing:
        seen, questions = set(), []
        for item in missing:
            if item not in seen and item in QUESTIONS:
                seen.add(item)
                questions.append(QUESTIONS[item])
        raise NeedsClarification(questions or ["Could not interpret that schedule."])

    tier = instance.get("tier")
    units = int(proposal["units"])

    # Refusals -- the request cannot be satisfied, so a question is pointless.
    check_tier_schedulable(tier)
    check_units_for_tier(units, tier)
    check_units_for_zones(units, instance.get("zoneCount") or 0)

    up, down = parse_hhmm(proposal["scaleUpAt"]), parse_hhmm(proposal["scaleDownAt"])
    if down <= up:
        raise ProfileError("the scale-down time (%s) must be after the scale-up "
                           "time (%s)" % (proposal["scaleDownAt"], proposal["scaleUpAt"]))
    needed = min_window_minutes(tier=tier)
    if (down - up) < needed:
        raise ProfileError(
            "that window is %d minutes, but an APIM scale takes 15-45 minutes each "
            "way, so it needs at least %d minutes to be worth doing"
            % (down - up, needed))

    return {
        "name": name,
        "target": {"subscription": instance["subscription"],
                   "resourceGroup": instance["resourceGroup"],
                   "service": instance["service"]},
        "timezone": tz,
        "days": list(proposal.get("days") or list(DAY_NAMES)),
        "scaleUpAt": proposal["scaleUpAt"],
        "scaleDownAt": proposal["scaleDownAt"],
        "units": units,
        # DESIGN.md 5.1 -- read off the instance, never from the model.
        "baselineUnits": int(instance.get("capacity") or 1),
        "prewarmMinutes": DEFAULT_PREWARM_MINUTES if prewarm is None else int(prewarm),
        "endDate": None,
        "paused": False,
        "sourceText": " ".join(text.split()),
        "notes": proposal.get("notes", ""),
    }
