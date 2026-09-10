"""
APIM instance discovery.

Read-only. Supplies the inventory the interpreter picks from, so a prompt can
only name an instance that exists, and the unit count can be checked against
that instance's tier and zone count before anything is stored.
"""

from __future__ import annotations

import json
import shutil
import subprocess


class DiscoveryError(RuntimeError):
    pass


def _az(args, timeout=120):
    if shutil.which("az") is None:
        raise DiscoveryError("the Azure CLI ('az') is not installed or not on PATH")
    proc = subprocess.run(["az"] + args + ["-o", "json"],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise DiscoveryError((proc.stderr or proc.stdout).strip()[:400])
    out = proc.stdout.strip()
    return json.loads(out) if out else None


class Discovery:
    def instances(self):
        """-> [{service, resourceGroup, subscription, location, tier, capacity,
                zoneCount, provisioningState, targetProvisioningState}]"""
        rows = _az(["apim", "list", "--query",
                    "[].{service:name,resourceGroup:resourceGroup,id:id,"
                    "location:location,tier:sku.name,capacity:sku.capacity,"
                    "zones:zones,provisioningState:provisioningState,"
                    "targetProvisioningState:targetProvisioningState}"]) or []
        for row in rows:
            rid = row.pop("id", "") or ""
            parts = rid.split("/")
            row["subscription"] = parts[2] if len(parts) > 2 else ""
            row["zoneCount"] = len(row.pop("zones", None) or [])
        return rows

    def resolve(self, service_name, resource_group=None):
        """Find exactly one instance, or explain why it cannot."""
        rows = self.instances()
        hits = [r for r in rows
                if r["service"].lower() == (service_name or "").lower()
                and (not resource_group
                     or r["resourceGroup"].lower() == resource_group.lower())]
        if not hits:
            known = ", ".join(sorted(r["service"] for r in rows)) or "(none)"
            raise DiscoveryError(
                "no API Management instance named %r. Available: %s"
                % (service_name, known))
        if len(hits) > 1:
            raise DiscoveryError(
                "%r matches %d instances; name the resource group too"
                % (service_name, len(hits)))
        return hits[0]
