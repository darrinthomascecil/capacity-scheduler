"""
Applying a unit count to an APIM instance.

Two things make this unlike the AKS executor, and both come straight from the
docs (DESIGN.md 3.1, 3.2):

  * a scale takes 15-45 minutes, so an issued scale is not a finished scale;
  * the service LOCKS while it changes, so issuing a second PATCH into an
    in-flight operation just produces a failure.

Everything here exists to avoid hammering a locked service.
"""

from __future__ import annotations

import json
import shutil
import subprocess

API_VERSION = "2024-05-01"
ARM = "https://management.azure.com"


class ExecutionError(RuntimeError):
    pass


def _az(args, timeout=300):
    if shutil.which("az") is None:
        raise ExecutionError("the Azure CLI ('az') is not installed or not on PATH")
    proc = subprocess.run(["az"] + args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ExecutionError((proc.stderr or proc.stdout).strip()[:400])
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def resource_id(target):
    return ("/subscriptions/%s/resourceGroups/%s/providers/Microsoft.ApiManagement"
            "/service/%s" % (target["subscription"], target["resourceGroup"],
                             target["service"]))


def observe(target):
    """Current state. -> {capacity, tier, zoneCount, provisioningState,
                          targetProvisioningState, inFlight}"""
    body = _az(["rest", "--method", "get",
                "--url", "%s%s?api-version=%s" % (ARM, resource_id(target), API_VERSION),
                "-o", "json"]) or {}
    sku = body.get("sku") or {}
    properties = body.get("properties") or {}
    target_state = properties.get("targetProvisioningState") or ""
    return {
        "capacity": sku.get("capacity"),
        "tier": sku.get("name"),
        "zoneCount": len(body.get("zones") or []),
        "provisioningState": properties.get("provisioningState"),
        "targetProvisioningState": target_state,
        # DESIGN.md 3.2 -- a non-empty target state means an operation is running.
        "inFlight": bool(target_state),
    }


def apply(target, units, dry_run=False, observed=None):
    """Set sku.capacity.

    -> {changed, before, after, skipped, command}

    Never writes sku.name. Changing tier can silently remove VNet integration or
    multi-region (DESIGN.md 7), which is not something a schedule does at 6am --
    but the PATCH body must still carry the CURRENT tier, because sku is
    replaced wholesale.
    """
    observed = observed if observed is not None else observe(target)

    if observed.get("inFlight"):
        return {"changed": False, "before": observed, "after": None,
                "skipped": "an operation is already in flight (targetProvisioningState=%r); "
                           "APIM locks during changes"
                           % observed["targetProvisioningState"],
                "command": None}

    state = observed.get("provisioningState")
    if state and state != "Succeeded":
        return {"changed": False, "before": observed, "after": None,
                "skipped": "provisioningState is %r, not 'Succeeded'" % state,
                "command": None}

    if observed.get("capacity") == units:
        return {"changed": False, "before": observed, "after": observed,
                "skipped": None, "command": None}

    tier = observed.get("tier")
    if not tier:
        raise ExecutionError("could not read the instance's current tier; refusing "
                             "to PATCH sku without it")

    body = json.dumps({"sku": {"name": tier, "capacity": units}})
    command = ["rest", "--method", "patch",
               "--url", "%s%s?api-version=%s" % (ARM, resource_id(target), API_VERSION),
               "--headers", "Content-Type=application/json",
               "--body", body]

    if dry_run:
        return {"changed": False, "before": observed, "after": None,
                "skipped": "dry run", "command": "az " + " ".join(command)}

    _az(command + ["-o", "none"])
    # 202 Accepted. The scale is now RUNNING, not done -- the worker observes
    # completion on a later tick and records how long it took (DESIGN.md D1/D2).
    return {"changed": True, "before": observed, "after": None,
            "skipped": None, "command": "az " + " ".join(command)}
