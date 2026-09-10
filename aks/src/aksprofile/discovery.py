"""
Cluster and node pool discovery (F13, F14, F15).

The spec calls for MCP discovery. No MCP server is available to depend on here,
so discovery goes through the Azure CLI behind a small interface -- swapping in
an MCP backend later means implementing `Discovery` and changing one line in
cli.py, with no other caller touched.
"""

from __future__ import annotations

import json
import shutil
import subprocess


class DiscoveryError(RuntimeError):
    pass


def _az(args, timeout=90):
    if shutil.which("az") is None:
        raise DiscoveryError("the Azure CLI ('az') is not installed or not on PATH")
    proc = subprocess.run(["az"] + args + ["-o", "json"],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise DiscoveryError((proc.stderr or proc.stdout).strip()[:400])
    out = proc.stdout.strip()
    return json.loads(out) if out else None


class Discovery:
    """Read-only. Never mutates anything."""

    def clusters(self):
        """-> [{name, resourceGroup, subscription, location, powerState}]"""
        rows = _az(["aks", "list", "--query",
                    "[].{name:name,resourceGroup:resourceGroup,subscription:id,"
                    "location:location,powerState:powerState.code}"]) or []
        for row in rows:
            rid = row.get("subscription") or ""
            parts = rid.split("/")
            row["subscription"] = parts[2] if len(parts) > 2 else ""
        return rows

    def node_pools(self, cluster, resource_group):
        """-> [{name, mode, count, min, max, autoscale, vmSize}]"""
        rows = _az(["aks", "nodepool", "list",
                    "--cluster-name", cluster, "--resource-group", resource_group,
                    "--query",
                    "[].{name:name,mode:mode,count:count,min:minCount,max:maxCount,"
                    "autoscale:enableAutoScaling,vmSize:vmSize}"]) or []
        return rows

    def inventory(self):
        """Every cluster/pool pair visible to this identity.

        -> [{cluster, resourceGroup, subscription, nodePool, mode, count,
             autoscale, vmSize}]
        """
        rows = []
        for cluster in self.clusters():
            for pool in self.node_pools(cluster["name"], cluster["resourceGroup"]):
                rows.append({
                    "cluster": cluster["name"],
                    "resourceGroup": cluster["resourceGroup"],
                    "subscription": cluster["subscription"],
                    "nodePool": pool["name"],
                    "mode": pool.get("mode"),
                    "count": pool.get("count"),
                    "autoscale": pool.get("autoscale"),
                    "vmSize": pool.get("vmSize"),
                })
        return rows

    def resolve_target(self, cluster_name, pool_name):
        """Find exactly one cluster/pool pair, or explain why it can't.

        Ambiguity rule 3 (DESIGN.md section 4): unknown or multiply-matching names
        are a question, not a guess.
        """
        clusters = [c for c in self.clusters()
                    if c["name"].lower() == (cluster_name or "").lower()]
        if not clusters:
            known = ", ".join(sorted(c["name"] for c in self.clusters())) or "(none)"
            raise DiscoveryError(
                "no cluster named %r. Available: %s" % (cluster_name, known))
        if len(clusters) > 1:
            raise DiscoveryError(
                "cluster name %r matches %d clusters; qualify it by resource group"
                % (cluster_name, len(clusters)))

        cluster = clusters[0]
        pools = self.node_pools(cluster["name"], cluster["resourceGroup"])
        hits = [p for p in pools if p["name"].lower() == (pool_name or "").lower()]
        if not hits:
            known = ", ".join(sorted(p["name"] for p in pools)) or "(none)"
            raise DiscoveryError(
                "cluster %r has no node pool named %r. Available: %s"
                % (cluster["name"], pool_name, known))

        pool = hits[0]
        return {
            "target": {
                "subscription": cluster["subscription"],
                "resourceGroup": cluster["resourceGroup"],
                "cluster": cluster["name"],
                "nodePool": pool["name"],
            },
            "pool": pool,
        }
