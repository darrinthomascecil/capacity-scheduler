"""
The worker.

Imports model, store, bounds, executor. It does NOT import the interpreter, and
never re-reads `sourceText` -- run this with no model credentials and it behaves
identically.

Two APIM-specific responsibilities beyond the AKS equivalent:

  * refuse to act while a scale is in flight (DESIGN.md 3.2);
  * measure how long each scale actually took, so pre-warm can be based on data
    rather than the docs' "15-45 minutes, it depends" (DESIGN.md D1/D2).
"""

from __future__ import annotations

import datetime as _dt
import json
import signal
import sys
import time

from . import bounds as bounds_mod
from . import config
from .executor import ExecutionError, apply, observe
from .model import ProfileError, clamp, resolve
from .store import open_store

DEFAULT_TICK_SECONDS = 300     # DESIGN.md 3.3


def _log(record):
    print(json.dumps(record, separators=(",", ":")), flush=True)


class _Stopper:
    def __init__(self):
        self.stopping = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except ValueError:
                pass

    def _handle(self, signum, _frame):
        _log({"event": "worker.shutdown", "signal": int(signum)})
        self.stopping = True

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))


def _beat(path=None):
    try:
        with open(path or config.get("APIMPROFILE_HEARTBEAT"), "w") as handle:
            handle.write(_dt.datetime.now(_dt.timezone.utc).isoformat())
    except OSError:
        pass


def _settle_pending(store, profile_name, observed, now):
    """If a scale we issued has finished, record how long it took.

    This is the whole of D1: no polling, no operation URLs -- just noticing that
    the instance stopped being in-flight and reached the number we asked for.
    """
    state = store.get_state(profile_name) or {}
    pending = state.get("pending")
    if not pending:
        return None
    if observed.get("inFlight"):
        return None
    if observed.get("capacity") != pending.get("to"):
        # Finished, but not where we asked -- someone else moved it. Drop the
        # measurement rather than record a misleading duration.
        store.set_state(profile_name, {})
        return None
    issued = _dt.datetime.fromisoformat(pending["issuedAt"])
    duration = int((now - issued).total_seconds())
    store.set_state(profile_name, {})
    return {"from": pending.get("from"), "to": pending.get("to"),
            "seconds": duration}


def tick(store, now=None, dry_run=False, bounds_path=None):
    now = now or _dt.datetime.now(_dt.timezone.utc)
    table = bounds_mod.load(bounds_path)
    records = []

    for profile in store.list():
        target = profile["target"]
        record = {
            "ts": now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile": profile["name"],
            "target": "%s/%s" % (target["resourceGroup"], target["service"]),
            "desired": None, "observed": None,
            "action": "error", "clamped": False, "dryRun": dry_run, "reason": "",
        }
        duration = None
        try:
            decision = resolve(profile, now)
            record["reason"] = decision["reason"]
            if not decision["active"]:
                record["action"] = "inactive"
                records.append(record)
                continue

            observed = observe(target)
            record["observed"] = observed.get("capacity")

            settled = _settle_pending(store, profile["name"], observed, now)
            if settled:
                duration = settled["seconds"]
                record["reason"] += ("; previous scale %s->%s took %dm%02ds"
                                     % (settled["from"], settled["to"],
                                        duration // 60, duration % 60))

            pool_bounds, defaulted = bounds_mod.for_target(target, table)
            units, was_clamped, note = clamp(
                decision["units"], pool_bounds,
                tier=observed.get("tier"), zone_count=observed.get("zoneCount") or 0)
            record["desired"] = units
            record["clamped"] = was_clamped
            if was_clamped:
                record["reason"] += "; " + note
            if defaulted:
                record["reason"] += "; no bounds configured, using fallback"

            result = apply(target, units, dry_run=dry_run, observed=observed)

            if result["changed"]:
                record["action"] = "issued"     # NOT "applied" -- see below
                record["reason"] += ("; scale issued, expect ~%d-%d minutes"
                                     % (15, 45))
                # Remember what we asked for, so the next tick can time it.
                store.set_state(profile["name"], {"pending": {
                    "issuedAt": now.astimezone(_dt.timezone.utc).isoformat(),
                    "from": observed.get("capacity"), "to": units}})
            elif result["skipped"]:
                record["action"] = ("inflight" if "in flight" in result["skipped"]
                                    else "skipped")
                record["reason"] += "; " + result["skipped"]
            else:
                record["action"] = "nochange"

        except (ExecutionError, ProfileError, ValueError) as exc:
            record["action"] = "error"
            record["reason"] = "%s: %s" % (type(exc).__name__, exc)

        store.record_run(profile["name"], record["action"], record["reason"], duration)
        records.append(record)

    return records


def run_forever(store, interval=DEFAULT_TICK_SECONDS, dry_run=False):
    stopper = _Stopper()
    _log({"event": "worker.start", "intervalSeconds": interval, "dryRun": dry_run})
    _beat()
    while not stopper.stopping:
        started = time.monotonic()
        try:
            for record in tick(store, dry_run=dry_run):
                _log(record)
            _beat()
        except Exception as exc:
            _log({"event": "worker.tick_failed",
                  "error": "%s: %s" % (type(exc).__name__, exc)})
        stopper.wait(max(0.0, interval - (time.monotonic() - started)))
    _log({"event": "worker.stopped"})


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    config.load_dotenv()
    dry_run = "--dry-run" in argv or config.get_bool("APIMPROFILE_DRY_RUN")
    once = "--once" in argv
    interval = config.get_int("APIMPROFILE_TICK_SECONDS")
    for index, arg in enumerate(argv):
        if arg == "--interval" and index + 1 < len(argv):
            interval = int(argv[index + 1])
    try:
        store = open_store()
    except (ValueError, RuntimeError) as exc:
        _log({"event": "worker.misconfigured", "error": str(exc)})
        return 2
    with store:
        if once:
            for record in tick(store, dry_run=dry_run):
                _log(record)
            return 0
        run_forever(store, interval=interval, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
