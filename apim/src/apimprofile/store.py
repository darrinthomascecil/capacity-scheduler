"""
Profile persistence.

    table://<storage-account>    Azure Table Storage -- the only durable backend
    memory://                    in-process, tests only

Beyond profiles and run history, this store carries a small amount of per-profile
STATE: what scale was issued and when. That is how the scheduler measures its own
scale duration (DESIGN.md D1) without polling anything.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading

from .model import ProfileError, validate

DEFAULT_TABLE = "apimprofiles"
DEFAULT_RUNS_TABLE = "apimprofileruns"
DEFAULT_STATE_TABLE = "apimprofilestate"


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class ConcurrentModification(RuntimeError):
    pass


class StoreBackend:
    durable = False

    def save(self, profile, etag=None): raise NotImplementedError
    def get(self, name): raise NotImplementedError
    def get_with_etag(self, name): raise NotImplementedError
    def list(self): raise NotImplementedError
    def delete(self, name): raise NotImplementedError
    def record_run(self, profile, action, detail=None, duration=None): raise NotImplementedError
    def recent_runs(self, limit=20): raise NotImplementedError
    def get_state(self, name): raise NotImplementedError
    def set_state(self, name, state): raise NotImplementedError
    def close(self): pass

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()

    def set_paused(self, name, paused):
        profile, etag = self.get_with_etag(name)
        if profile is None:
            raise ProfileError("no profile named %r" % name)
        profile["paused"] = bool(paused)
        return self.save(profile, etag=etag)

    def conflicting(self, profile):
        """Other active profiles on the same instance. APIM locks during a
        scale, so two profiles on one instance is worse here than on AKS -- they
        would spend the day fighting through 30-minute operations."""
        target = profile["target"]
        key = (target["resourceGroup"], target["service"])
        return [o["name"] for o in self.list()
                if o["name"] != profile["name"] and not o.get("paused")
                and (o["target"]["resourceGroup"], o["target"]["service"]) == key]

    def scale_durations(self, limit=50):
        """Observed scale durations, newest first -- the data behind D1."""
        return [r["duration"] for r in self.recent_runs(limit)
                if r.get("duration")]


class MemoryStore(StoreBackend):
    """Tests only. Cannot satisfy durability; `open_store` will not pick it
    unless asked for by name."""

    durable = False

    def __init__(self, *_a, **_kw):
        self._p, self._e, self._runs, self._state = {}, {}, [], {}
        self._lock = threading.Lock()
        self._n = 0

    def save(self, profile, etag=None):
        validate(profile)
        name = profile["name"]
        with self._lock:
            current = self._e.get(name)
            if current is not None and etag is not None and str(etag) != str(current):
                raise ConcurrentModification("%r changed since it was read" % name)
            existing = self._p.get(name)
            profile["createdAt"] = (json.loads(existing)["createdAt"] if existing
                                    else profile.get("createdAt") or _now())
            self._n += 1
            self._p[name] = json.dumps(profile)
            self._e[name] = str(self._n)
        return profile

    def get(self, name): return self.get_with_etag(name)[0]

    def get_with_etag(self, name):
        body = self._p.get(name)
        return (json.loads(body), self._e.get(name)) if body else (None, None)

    def list(self): return [json.loads(b) for b in self._p.values()]

    def delete(self, name):
        with self._lock:
            existed = self._p.pop(name, None) is not None
            self._e.pop(name, None)
        return existed

    def record_run(self, profile, action, detail=None, duration=None):
        self._runs.append({"profile": profile, "at": _now(), "action": action,
                           "detail": detail or "", "duration": duration})

    def recent_runs(self, limit=20): return list(reversed(self._runs))[:limit]
    def get_state(self, name): return dict(self._state.get(name) or {})
    def set_state(self, name, state): self._state[name] = dict(state or {})


class TableStore(StoreBackend):
    PARTITION = "profile"
    durable = True

    def __init__(self, account, credential=None, endpoint=None):
        try:
            from azure.data.tables import TableServiceClient
        except ImportError:
            raise RuntimeError("the table backend needs `pip install -r requirements.txt`")
        if credential is None:
            from azure.identity import DefaultAzureCredential
            credential = DefaultAzureCredential()
        endpoint = endpoint or "https://%s.table.core.windows.net" % account
        svc = TableServiceClient(endpoint=endpoint, credential=credential)
        self.account = account
        self.profiles = svc.create_table_if_not_exists(DEFAULT_TABLE)
        self.runs = svc.create_table_if_not_exists(DEFAULT_RUNS_TABLE)
        self.state = svc.create_table_if_not_exists(DEFAULT_STATE_TABLE)

    def _entity(self, profile, created):
        t = profile["target"]
        return {"PartitionKey": self.PARTITION, "RowKey": profile["name"],
                "body": json.dumps(profile), "created_at": created,
                "updated_at": _now(),
                "target": "%s/%s" % (t.get("resourceGroup"), t["service"]),
                "paused": bool(profile.get("paused"))}

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
        entity = self._entity(profile, created)
        if existing is None:
            self.profiles.create_entity(entity)
            return profile
        try:
            self.profiles.update_entity(entity, mode=UpdateMode.REPLACE,
                                        etag=etag or existing.metadata.get("etag"),
                                        match_condition=MatchConditions.IfNotModified)
        except ResourceModifiedError:
            raise ConcurrentModification("%r changed since it was read" % name)
        return profile

    def get(self, name): return self.get_with_etag(name)[0]

    def get_with_etag(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            e = self.profiles.get_entity(self.PARTITION, name)
        except ResourceNotFoundError:
            return None, None
        return json.loads(e["body"]), e.metadata.get("etag")

    def list(self):
        rows = list(self.profiles.query_entities("PartitionKey eq '%s'" % self.PARTITION))
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
        t = profile["target"]
        key = "%s/%s" % (t.get("resourceGroup"), t["service"])
        rows = self.profiles.query_entities(
            "PartitionKey eq '%s' and target eq '%s' and paused eq false"
            % (self.PARTITION, key))
        return [e["RowKey"] for e in rows if e["RowKey"] != profile["name"]]

    def record_run(self, profile, action, detail=None, duration=None):
        at = _now()
        stamp = int(_dt.datetime.fromisoformat(at).timestamp() * 1000)
        self.runs.create_entity({
            "PartitionKey": profile, "RowKey": "%020d" % (99999999999999 - stamp),
            "at": at, "action": action, "detail": detail or "",
            "duration": duration or 0})

    def recent_runs(self, limit=20):
        rows = [{"profile": e["PartitionKey"], "at": e.get("at"),
                 "action": e.get("action"), "detail": e.get("detail"),
                 "duration": e.get("duration") or None}
                for e in self.runs.list_entities()]
        rows.sort(key=lambda r: r["at"] or "", reverse=True)
        return rows[:limit]

    def get_state(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            e = self.state.get_entity("state", name)
        except ResourceNotFoundError:
            return {}
        return json.loads(e.get("body") or "{}")

    def set_state(self, name, state):
        from azure.data.tables import UpdateMode
        self.state.upsert_entity({"PartitionKey": "state", "RowKey": name,
                                  "body": json.dumps(state or {}), "at": _now()},
                                 mode=UpdateMode.REPLACE)


def parse_url(url):
    if url.startswith("table://"):
        location = url[len("table://"):].strip("/")
        if not location:
            raise ValueError("table:// needs a storage account name")
        return "table", location
    if url.startswith("memory://"):
        return "memory", ""
    raise ValueError("unsupported store %r (expected table://<account>, or "
                     "memory:// for tests)" % url)


def open_store(url=None, **kwargs):
    from . import config
    if url is None:
        url = config.get("APIMPROFILE_STORE")
    if not url:
        raise ValueError("no store configured. Set "
                         "APIMPROFILE_STORE=table://<storage-account>")
    scheme, location = parse_url(url)
    return TableStore(location, **kwargs) if scheme == "table" else MemoryStore(**kwargs)
