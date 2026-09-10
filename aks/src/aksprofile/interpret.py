"""
Natural language -> profile, ONCE, at creation time (F7).

Nothing in the execution path imports this module. It runs when a person types a
sentence, and never again -- the worker reads the structured result (F8).

The model is pluggable: provider and model name come from the environment, and
no other module depends on which one is used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from .model import DAY_NAMES, MODES

from . import config


def _model():
    return config.get("AKSPROFILE_MODEL")


def _base_url():
    return config.get("AKSPROFILE_BASE_URL")


def _api_key_var():
    return config.get("AKSPROFILE_API_KEY_VAR")


class InterpretError(RuntimeError):
    pass


class NeedsClarification(Exception):
    """The prompt is missing something. Ask, do not guess (F11, F12)."""

    def __init__(self, questions):
        self.questions = questions
        super().__init__("; ".join(questions))


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["windows", "otherwise", "mode", "timezone", "cluster",
                 "nodePool", "requestedPool", "missing", "notes"],
    "properties": {
        "windows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["days", "start", "end", "value"],
                "properties": {
                    "days": {"type": "array",
                             "items": {"type": "string", "enum": list(DAY_NAMES)}},
                    "start": {"type": "string", "description": "24-hour HH:MM"},
                    "end": {"type": "string", "description": "24-hour HH:MM, after start"},
                    "value": {"type": "integer", "description": "nodes during this window"},
                },
            },
        },
        "otherwise": {"type": "integer",
                      "description": "nodes outside every window -- the scale-down value"},
        "mode": {"type": "string", "enum": list(MODES),
                 "description": "'minimum' if the text says minimum/at least/floor; "
                                "'count' for a bare node count"},
        "timezone": {"type": "string", "description": "IANA name, or empty if not determinable"},
        "cluster": {
            "type": "string",
            "description": "The cluster to scale. MUST be one of the cluster names "
                           "listed in the user message, or empty if the text does "
                           "not identify one. Never invent a name.",
        },
        "nodePool": {
            "type": "string",
            "description": "The node pool to scale. MUST be one of the pool names "
                           "listed for that cluster, or empty if the text does not "
                           "identify one. Never invent a name.",
        },
        "requestedPool": {
            "type": "string",
            "description": "The pool name the text actually asked for, VERBATIM, "
                           "even if it is not in the inventory. Empty only if the "
                           "text names no pool at all. This is used to write a "
                           "helpful error, so report it even when nodePool is empty.",
        },
        "missing": {
            "type": "array",
            "items": {"type": "string", "enum": [
                "scale_down_value", "scale_down_time", "timezone",
                "node_count", "mode", "target"]},
            "description": "Anything you could NOT determine. Leave empty if the "
                           "request is complete. Never guess to avoid listing here.",
        },
        "notes": {"type": "string", "description": "Assumptions you made."},
    },
}

SYSTEM_PROMPT = """\
You convert plain-English AKS node pool scaling schedules into structured windows.

Rules:
- `start`/`end` are 24-hour HH:MM wall-clock times. `end` must be AFTER `start`.
  An overnight period is expressed as the `otherwise` value, not as a window
  that wraps midnight.
- `otherwise` is what the pool runs outside every window -- the scale-down value.
- `mode` is "minimum" when the text says minimum / at least / floor / no fewer
  than. It is "count" for a bare number of nodes. This distinction matters: it
  is the difference between an autoscaler floor and an exact node count.
- "weekday" means Mon-Fri. "weekend" means Sat-Sun. If days are unstated, use
  all seven.
- Report anything you cannot determine in `missing`. Do NOT invent a scale-down
  value or time to avoid listing it -- a schedule with no scale-down is
  incomplete and the caller must ask.
- Use only IANA timezone names.
- `cluster` and `nodePool` must come from the inventory in the user message.
  If the text names a pool that is not in the inventory, or names none at all,
  or the name is ambiguous across clusters, leave both empty and put "target"
  in `missing`. Never guess which pool the user meant.
- `requestedPool` is what the text asked for verbatim, even when that pool does
  not exist. Report it whenever the text names a pool at all.
"""

QUESTIONS = {
    "scale_down_value": "What should it scale down to?",
    "scale_down_time":  "What time should it scale down?",
    "timezone":         "Which timezone should this schedule use? (e.g. America/Los_Angeles)",
    "node_count":       "How many nodes exactly?",
    "mode":             "Is that an exact node count, or an autoscaler minimum? "
                        "(say 'minimum four' for a floor)",
    "target":           "Which cluster and node pool? Name it in the prompt, or "
                        "pass --cluster and --pool.",
}


AZURE_AI_HOSTS = (".cognitiveservices.azure.com", ".openai.azure.com",
                  ".services.ai.azure.com")
AZURE_AI_SCOPE = "https://cognitiveservices.azure.com"


def _is_azure_endpoint(url):
    return any(host in (url or "") for host in AZURE_AI_HOSTS)


def _azure_token():
    """An AAD token for an Azure AI endpoint.

    Lets the model call use the same identity as everything else -- `az login`
    locally, a managed identity when deployed -- so there is no separate API key
    to store or rotate, and no traffic leaving the subscription.
    """
    if shutil.which("az") is None:
        raise InterpretError(
            "the endpoint is an Azure AI resource, which authenticates with a "
            "token, but the Azure CLI is not available to get one")
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
    """-> the bearer token to use, however it is obtained."""
    explicit = os.environ.get(_api_key_var())
    if explicit:
        return explicit
    if _is_azure_endpoint(_base_url()):
        return _azure_token()
    raise InterpretError(
        "%s is not set. Either export it, or point AKSPROFILE_BASE_URL at an "
        "Azure AI endpoint to authenticate with your Azure identity instead."
        % _api_key_var())


def describe_inventory(inventory):
    lines = []
    by_cluster = {}
    for row in inventory:
        by_cluster.setdefault((row["cluster"], row["resourceGroup"]), []).append(row)
    for (cluster, group), pools in sorted(by_cluster.items()):
        lines.append("  cluster %s (resource group %s)" % (cluster, group))
        for pool in sorted(pools, key=lambda p: p["nodePool"]):
            lines.append("      node pool %s  (mode %s, %s nodes, autoscale %s)"
                         % (pool["nodePool"], pool.get("mode") or "?",
                            pool.get("count"), "on" if pool.get("autoscale") else "off"))
    return "\n".join(lines) or "  (none visible)"


def call_model(text, inventory=None, timeout=60):
    api_key = resolve_credential()

    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                "Clusters and node pools available:\n%s\n\nSchedule:\n%s"
                % (describe_inventory(inventory or []), text))},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "scaling_schedule", "strict": True, "schema": SCHEMA},
        },
    }
    request = urllib.request.Request(
        _base_url().rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer %s" % api_key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise InterpretError("HTTP %d: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:300]))
    except urllib.error.URLError as exc:
        raise InterpretError("network: %s" % exc.reason)
    try:
        return json.loads(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, ValueError) as exc:
        raise InterpretError("could not read model output: %s" % exc)


def resolve_target_from(proposal, inventory):
    """Match what the model named against pools that actually exist.

    Returns a target dict, or None if the prompt did not identify exactly one
    pool -- in which case the caller asks (ambiguity rule 3, DESIGN.md section 4).
    Matching is case-insensitive; a pool name unique across the whole inventory
    can be named without its cluster.
    """
    requested = (proposal.get("requestedPool") or "").strip()
    if not requested:
        # F14: the node pool must be EXPLICITLY targeted. With one pool visible a
        # model will helpfully pick it -- but then the same prompt silently means
        # something else the day a second pool appears. Ask instead.
        return None, None

    cluster = (proposal.get("cluster") or "").strip().lower()
    pool = (proposal.get("nodePool") or "").strip().lower()
    if not pool:
        # Named something that does not exist. Say what was asked for and what
        # is available -- far more useful than "which pool?".
        raise InterpretError(
            "no node pool named %r. Available: %s"
            % (requested, ", ".join(sorted(
                "%s/%s" % (r["cluster"], r["nodePool"]) for r in inventory))
                or "(none)"))

    hits = [row for row in inventory if row["nodePool"].lower() == pool
            and (not cluster or row["cluster"].lower() == cluster)]
    if len(hits) == 1:
        row = hits[0]
        return {"subscription": row["subscription"],
                "resourceGroup": row["resourceGroup"],
                "cluster": row["cluster"],
                "nodePool": row["nodePool"]}, row
    if not hits:
        raise InterpretError(
            "no node pool named %r%s. Available: %s"
            % (proposal.get("nodePool"),
               " on cluster %r" % proposal.get("cluster") if cluster else "",
               ", ".join(sorted("%s/%s" % (r["cluster"], r["nodePool"])
                                for r in inventory)) or "(none)"))
    raise InterpretError(
        "%r is ambiguous -- it matches %s. Name the cluster too."
        % (proposal.get("nodePool"),
           ", ".join(sorted("%s/%s" % (r["cluster"], r["nodePool"]) for r in hits))))


def to_profile(name, text, target, proposal, timezone=None):
    """`target` is already resolved by the caller, so any "target" the model
    reported as missing is stale -- drop it before deciding whether to ask."""
    """Turn a model proposal into a profile record, or raise NeedsClarification.

    Ambiguity rules are enforced HERE, in ordinary code -- not left to the
    model's judgement (DESIGN.md section 4)."""
    missing = [m for m in (proposal.get("missing") or []) if m != "target"]

    tz = timezone or (proposal.get("timezone") or "").strip()
    if not tz and "timezone" not in missing:
        missing.append("timezone")

    windows = proposal.get("windows") or []
    if not windows:
        missing.append("node_count")

    otherwise = proposal.get("otherwise")
    if otherwise is None and "scale_down_value" not in missing:
        missing.append("scale_down_value")

    if missing:
        seen, questions = set(), []
        for item in missing:
            if item not in seen and item in QUESTIONS:
                seen.add(item)
                questions.append(QUESTIONS[item])
        raise NeedsClarification(questions or ["Could not interpret that schedule."])

    return {
        "name": name,
        "target": target,
        "timezone": tz,
        "mode": proposal.get("mode") or "count",
        "windows": [{"days": w["days"], "start": w["start"], "end": w["end"],
                     "value": w["value"]} for w in windows],
        "otherwise": otherwise,
        "endDate": None,
        "paused": False,
        "sourceText": " ".join(text.split()),
        "notes": proposal.get("notes", ""),
    }


def interpret(name, text, target, timezone=None):
    return to_profile(name, text, target, call_model(text), timezone=timezone)
