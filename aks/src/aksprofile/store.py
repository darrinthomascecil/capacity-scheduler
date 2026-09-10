"""
Profile persistence (F5).

    table://<storage-account>    Azure Table Storage — the only durable backend
    memory://                    in-process, for tests only. Loses everything.

Azure Table Storage is durable, serverless, costs pennies at this volume, and
supports ETag optimistic concurrency so a lost update becomes an error rather
than a silent clobber. Authentication is DefaultAzureCredential: a managed
identity when deployed, `az login` locally. No connection strings, no keys.

The Azure SDK is imported lazily, so the memory backend — and therefore the
whole test suite — needs nothing installed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading

from .model import ProfileError, validate

DEFAULT_TABLE_NAME = "aksprofiles"
DEFAULT_RUNS_TABLE = "aksprofileruns"


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class ConcurrentModification(RuntimeError):
    """Someone else wrote this profile since it was read."""


# ==========================================================================
# interface
# ==========================================================================

class StoreBackend:
    """Records only. A store never interprets language and never calls ARM."""

    durable = False

    def save(self, profile, etag=None): raise NotImplementedError
    def get(self, name): raise NotImplementedError
    def get_with_etag(self, name): raise NotImplementedError
    def list(self): raise NotImplementedError
    def delete(self, name): raise NotImplementedError
    def record_run(self, profile_name, action, detail=None): raise NotImplementedError
    def recent_runs(self, limit=20): raise NotImplementedError
    def close(self): pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- behaviour shared by every backend --------------------------------

    def set_paused(self, name, paused):
        profile, etag = self.get_with_etag(name)
        if profile is None:
            raise ProfileError("no profile named %r" % name)
        profile["paused"] = bool(paused)
        return self.save(profile, etag=etag)

    def conflicting(self, profile):
        """Other active profiles pointing at the same node pool (DESIGN.md 7)."""
        target = profile["target"]
        key = (target["cluster"], target["nodePool"], target.get("resourceGroup"))
        hits = []
        for other in self.list():
            if other["name"] == profile["name"] or other.get("paused"):
                continue
            ot = other["target"]
            if (ot["cluster"], ot["nodePool"], ot.get("resourceGroup")) == key:
                hits.append(other["name"])
        return hits


# ==========================================================================
# azure table storage — the deployed backend
# ==========================================================================

class TableStore(StoreBackend):
    PARTITION = "profile"
    durable = True

    def __init__(self, account, table=None, runs_table=None, credential=None,
                 endpoint=None):
        try:
            from azure.data.tables import TableServiceClient
        except ImportError:
            raise RuntimeError(
                "the table backend needs `pip install -r requirements.txt` "
                "(azure-data-tables, azure-identity)")
        if credential is None:
            from azure.identity import DefaultAzureCredential
            credential = DefaultAzureCredential()

        endpoint = endpoint or "https://%s.table.core.windows.net" % account
        service = TableServiceClient(endpoint=endpoint, credential=credential)
        self.account = account
        self.profiles = service.create_table_if_not_exists(table or DEFAULT_TABLE_NAME)
        self.runs = service.create_table_if_not_exists(runs_table or DEFAULT_RUNS_TABLE)

    @classmethod
    def _entity(cls, profile, created):
        return {
            "PartitionKey": cls.PARTITION,
            "RowKey": profile["name"],
            "body": json.dumps(profile),
            "created_at": created,
            "updated_at": _now(),
            # Denormalised so a conflict query does not parse every body.
            "target": "%s/%s/%s" % (profile["target"].get("resourceGroup"),
                                    profile["target"]["cluster"],
                                    profile["target"]["nodePool"]),
            "paused": bool(profile.get("paused")),
        }

    def save(self, profile, etag=None):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        validate(profile)
        name = profile["name"]
        try:
            existing = self.profiles.get_entity(self.PARTITION, name)
            created = existing.get("created_at") or _now()
        except ResourceNotFoundError:
            existing, created = None, profile.get("createdAt") or _now()

        profile["createdAt"] = created
        # Carried in the body, not just as a table column: the worker needs it to
        # break ties between profiles on one target, and it must read the same
        # on every backend.
        profile["updatedAt"] = _now()
        entity = self._entity(profile, created)

        if existing is None:
            self.profiles.create_entity(entity)
            return profile
        try:
            self.profiles.update_entity(
                entity, mode=UpdateMode.REPLACE,
                etag=etag or existing.metadata.get("etag"),
                match_condition=MatchConditions.IfNotModified)
        except ResourceModifiedError:
            raise ConcurrentModification("%r changed since it was read" % name)
        return profile

    def get(self, name):
        return self.get_with_etag(name)[0]

    def get_with_etag(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            entity = self.profiles.get_entity(self.PARTITION, name)
        except ResourceNotFoundError:
            return None, None
        return json.loads(entity["body"]), entity.metadata.get("etag")

    def list(self):
        rows = list(self.profiles.query_entities(
            "PartitionKey eq '%s'" % self.PARTITION))
        rows.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
        return [json.loads(e["body"]) for e in rows]

    def delete(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            self.profiles.delete_entity(self.PARTITION, name)
            return True
        except ResourceNotFoundError:
            return False

    def conflicting(self, profile):
        """Server-side query on the denormalised target — no full scan."""
        target = profile["target"]
        key = "%s/%s/%s" % (target.get("resourceGroup"), target["cluster"],
                            target["nodePool"])
        rows = self.profiles.query_entities(
            "PartitionKey eq '%s' and target eq '%s' and paused eq false"
            % (self.PARTITION, key))
        return [e["RowKey"] for e in rows if e["RowKey"] != profile["name"]]

    def record_run(self, profile_name, action, detail=None):
        at = _now()
        stamp = int(_dt.datetime.fromisoformat(at).timestamp() * 1000)
        self.runs.create_entity({
            "PartitionKey": profile_name,
            # Descending row key, so "most recent" is a top-N scan.
            "RowKey": "%020d" % (99999999999999 - stamp),
            "at": at, "action": action, "detail": detail or "",
        })

    def recent_runs(self, limit=20):
        rows = []
        for entity in self.runs.list_entities():
            rows.append({"profile": entity["PartitionKey"], "at": entity.get("at"),
                         "action": entity.get("action"), "detail": entity.get("detail")})
        rows.sort(key=lambda r: r["at"] or "", reverse=True)
        return rows[:limit]


# ==========================================================================
# memory — tests only
# ==========================================================================

class MemoryStore(StoreBackend):
    """In-process. Everything is lost when the process exits.

    This exists so the test suite can exercise the backend contract without a
    storage account or a credential. It is NOT a deployment option: it cannot
    satisfy F5, and `open_store` will not select it unless asked for by name.
    """

    durable = False

    def __init__(self, *_args, **_kwargs):
        self._profiles = {}
        self._etags = {}
        self._runs = []
        self._lock = threading.Lock()
        self._counter = 0

    def save(self, profile, etag=None):
        validate(profile)
        name = profile["name"]
        with self._lock:
            current = self._etags.get(name)
            if current is not None and etag is not None and str(etag) != str(current):
                raise ConcurrentModification(
                    "%r changed since it was read (have %s, found %s)"
                    % (name, etag, current))
            existing = self._profiles.get(name)
            profile["createdAt"] = (json.loads(existing)["createdAt"] if existing
                                    else profile.get("createdAt") or _now())
            profile["updatedAt"] = _now()
            self._counter += 1
            self._profiles[name] = json.dumps(profile)
            self._etags[name] = str(self._counter)
        return profile

    def get(self, name):
        return self.get_with_etag(name)[0]

    def get_with_etag(self, name):
        body = self._profiles.get(name)
        if body is None:
            return None, None
        return json.loads(body), self._etags.get(name)

    def list(self):
        return [json.loads(b) for b in self._profiles.values()]

    def delete(self, name):
        with self._lock:
            existed = self._profiles.pop(name, None) is not None
            self._etags.pop(name, None)
        return existed

    def record_run(self, profile_name, action, detail=None):
        self._runs.append({"profile": profile_name, "at": _now(),
                           "action": action, "detail": detail or ""})

    def recent_runs(self, limit=20):
        return list(reversed(self._runs))[:limit]


# ==========================================================================
# factory
# ==========================================================================

def parse_url(url):
    """-> (scheme, location). Raises on anything unrecognised."""
    if url.startswith("table://"):
        location = url[len("table://"):].strip("/")
        if not location:
            raise ValueError("table:// needs a storage account name")
        return "table", location
    if url.startswith("memory://"):
        return "memory", ""
    raise ValueError(
        "unsupported store %r (expected table://<account>, or memory:// for tests)" % url)


def open_store(url=None, **kwargs):
    """Open the configured backend.

    Precedence: explicit url > AKSPROFILE_STORE. There is no default — a store
    that silently falls back to something non-durable is how F5 gets violated
    without anyone noticing.
    """
    if url is None:
        url = os.environ.get("AKSPROFILE_STORE")
    if not url:
        raise ValueError(
            "no store configured. Set AKSPROFILE_STORE=table://<storage-account>")

    scheme, location = parse_url(url)
    if scheme == "table":
        return TableStore(location, **kwargs)
    return MemoryStore(**kwargs)
