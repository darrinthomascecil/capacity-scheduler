"""
Applying a resolved value to a node pool, via the Azure CLI (F16, F17).

Authentication is whatever `az` is already logged in as -- one identity (F17).
This module makes no decisions: it is handed a value and told to apply it.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from .model import MODE_COUNT, MODE_MINIMUM


class ExecutionError(RuntimeError):
    pass


def _az(args, timeout=600):
    if shutil.which("az") is None:
        raise ExecutionError("the Azure CLI ('az') is not installed or not on PATH")
    proc = subprocess.run(["az"] + args + ["-o", "json"],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ExecutionError((proc.stderr or proc.stdout).strip()[:400])
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def observe(target):
    """Current state of the pool. -> {count, min, max, autoscale, mode, state}"""
    row = _az(["aks", "nodepool", "show",
               "--cluster-name", target["cluster"],
               "--resource-group", target["resourceGroup"],
               "--name", target["nodePool"],
               "--query", "{count:count,min:minCount,max:maxCount,"
                          "autoscale:enableAutoScaling,mode:mode,"
                          "state:provisioningState}"])
    return row or {}


def apply(target, value, mode, dry_run=False, observed=None):
    """Set the pool to `value`.

    mode 'count'   -> exact node count (`az aks nodepool scale`)
    mode 'minimum' -> autoscaler floor (`az aks nodepool update --min-count`)

    -> {changed, before, after, skipped, command}
    """
    observed = observed if observed is not None else observe(target)
    state = observed.get("state")
    if state and state != "Succeeded":
        return {"changed": False, "before": observed, "after": None,
                "skipped": "provisioningState is %r, not 'Succeeded'" % state,
                "command": None}

    if mode == MODE_MINIMUM:
        if not observed.get("autoscale"):
            return {"changed": False, "before": observed, "after": None,
                    "skipped": "'minimum' requires the cluster autoscaler, which is "
                               "disabled on this pool",
                    "command": None}
        if observed.get("min") == value:
            return {"changed": False, "before": observed, "after": observed,
                    "skipped": None, "command": None}
        command = ["aks", "nodepool", "update",
                   "--cluster-name", target["cluster"],
                   "--resource-group", target["resourceGroup"],
                   "--name", target["nodePool"],
                   "--update-cluster-autoscaler",
                   "--min-count", str(value),
                   "--max-count", str(observed.get("max") or value)]
    elif mode == MODE_COUNT:
        if observed.get("autoscale"):
            return {"changed": False, "before": observed, "after": None,
                    "skipped": "exact count requested but the autoscaler is enabled; "
                               "use 'minimum N' or disable autoscaling",
                    "command": None}
        if observed.get("count") == value:
            return {"changed": False, "before": observed, "after": observed,
                    "skipped": None, "command": None}
        command = ["aks", "nodepool", "scale",
                   "--cluster-name", target["cluster"],
                   "--resource-group", target["resourceGroup"],
                   "--name", target["nodePool"],
                   "--node-count", str(value)]
    else:
        raise ExecutionError("unknown mode %r" % mode)

    if dry_run:
        return {"changed": False, "before": observed, "after": None,
                "skipped": "dry run", "command": "az " + " ".join(command)}

    _az(command)
    return {"changed": True, "before": observed, "after": observe(target),
            "skipped": None, "command": "az " + " ".join(command)}
