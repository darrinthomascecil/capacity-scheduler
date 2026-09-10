"""
The worker (F8, F18).

It reads stored profiles, resolves them against the clock, clamps, and applies.

It imports model, store, bounds and executor. It does NOT import `interpret`,
and it never sees `sourceText` except to log it. That is acceptance criterion 4
made structural: run this with no model credentials configured and it behaves
identically, because there is no code path that could call a model.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import signal
import sys
import time

from . import bounds as bounds_mod
from . import config
from .executor import ExecutionError, apply, observe
from .model import clamp, resolve
from .store import open_store

DEFAULT_TICK_SECONDS = 60


class _Stopper:
    """Graceful shutdown. Kubernetes sends SIGTERM then waits; without this the
    process is killed mid-`az` call and a scale operation is left in flight."""

    def __init__(self):
        self.stopping = False
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except ValueError:
                pass  # not on the main thread (tests)

    def _handle(self, signum, _frame):
        _log({"event": "worker.shutdown", "signal": int(signum)})
        self.stopping = True

    def wait(self, seconds):
        """Sleep in short slices so a signal is noticed promptly."""
        deadline = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))


def _beat(path=None):
    """Touch a heartbeat file. The container HEALTHCHECK reads its age, so a
    wedged worker is distinguishable from a healthy idle one."""
    path = path or config.get("AKSPROFILE_HEARTBEAT")
    try:
        with open(path, "w") as handle:
            handle.write(_dt.datetime.now(_dt.timezone.utc).isoformat())
    except OSError:
        pass


def _log(record):
    print(json.dumps(record, separators=(",", ":")), flush=True)


def _target_key(profile):
    target = profile["target"]
    return (target.get("resourceGroup"), target["cluster"], target["nodePool"])


def select_winners(profiles):
    """One profile per target -- the most recently updated active one wins.

    DESIGN.md section 7. Without this the worker applies EVERY profile, so two
    actives on one node pool each undo the other on every tick. That is not
    hypothetical: it moved a live pool 1 -> 2 -> 1 mid-Scaling.

    Ordering is computed here rather than taken from store.list(), because list
    ordering is a backend implementation detail -- TableStore happens to sort by
    updated_at, MemoryStore does not.

    -> (winners, [(loser_profile, winner_name), ...])
    """
    by_target, suppressed = {}, []
    for profile in profiles:
        if profile.get("paused"):
            continue
        key = _target_key(profile)
        current = by_target.get(key)
        if current is None:
            by_target[key] = profile
            continue
        # Ties broken by name so the outcome is deterministic rather than
        # dependent on iteration order.
        challenger = (profile.get("updatedAt") or "", profile["name"])
        incumbent = (current.get("updatedAt") or "", current["name"])
        if challenger > incumbent:
            by_target[key] = profile
            suppressed.append((current, profile["name"]))
        else:
            suppressed.append((profile, current["name"]))
    return list(by_target.values()), suppressed


def tick(store, now=None, dry_run=False, bounds_path=None):
    """Evaluate every profile once. Returns a list of log records."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    table = bounds_mod.load(bounds_path)
    records = []

    stamp = now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    winners, suppressed = select_winners(store.list())

    # A suppressed profile must say so. Silently ignoring it trades thrashing for
    # a quieter failure: an active profile that does nothing, with no explanation.
    for loser, winner_name in suppressed:
        reason = ("another active profile targets %s/%s and was updated more "
                  "recently: %s. Pause or delete one of them."
                  % (loser["target"]["cluster"], loser["target"]["nodePool"],
                     winner_name))
        record = {"ts": stamp, "profile": loser["name"],
                  "target": "%s/%s" % (loser["target"]["cluster"],
                                       loser["target"]["nodePool"]),
                  "mode": loser.get("mode"), "desired": None,
                  "action": "superseded", "clamped": False, "dryRun": dry_run,
                  "reason": reason}
        store.record_run(loser["name"], "superseded", reason)
        records.append(record)

    # Paused profiles are excluded by select_winners, so report them here.
    for profile in store.list():
        if not profile.get("paused"):
            continue
        records.append({"ts": stamp, "profile": profile["name"],
                        "target": "%s/%s" % (profile["target"]["cluster"],
                                             profile["target"]["nodePool"]),
                        "mode": profile.get("mode"), "desired": None,
                        "action": "inactive", "clamped": False, "dryRun": dry_run,
                        "reason": "profile is paused"})

    for profile in winners:
        record = {
            "ts": now.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile": profile["name"],
            "target": "%s/%s" % (profile["target"]["cluster"],
                                 profile["target"]["nodePool"]),
            "mode": profile.get("mode"),
            "desired": None,
            "action": "error",
            "clamped": False,
            "dryRun": dry_run,
            "reason": "",
        }
        try:
            decision = resolve(profile, now)
            record["reason"] = decision["reason"]
            if not decision["active"]:
                record["action"] = "inactive"
                records.append(record)
                continue

            target = profile["target"]
            pool_bounds, defaulted = bounds_mod.for_target(target, table)
            observed = observe(target)
            is_system = (observed.get("mode") or "").lower() == "system"

            value, was_clamped, note = clamp(decision["value"], pool_bounds, is_system)
            record["desired"] = value
            record["clamped"] = was_clamped
            if was_clamped:
                record["reason"] += "; " + note
            if defaulted:
                record["reason"] += "; no bounds configured, using fallback"

            result = apply(target, value, decision["mode"],
                           dry_run=dry_run, observed=observed)
            if result["changed"]:
                record["action"] = "applied"
            elif result["skipped"]:
                record["action"] = "skipped"
                record["reason"] += "; " + result["skipped"]
            else:
                record["action"] = "nochange"

        except (ExecutionError, ValueError) as exc:
            record["action"] = "error"
            record["reason"] = "%s: %s" % (type(exc).__name__, exc)

        store.record_run(profile["name"], record["action"], record["reason"])
        records.append(record)

    return records


def run_forever(store, interval=DEFAULT_TICK_SECONDS, dry_run=False):
    stopper = _Stopper()
    _log({"event": "worker.start", "intervalSeconds": interval, "dryRun": dry_run,
          "store": (config.get("AKSPROFILE_STORE") or "").split("://")[0] or "unset"})
    _beat()
    while not stopper.stopping:
        started = time.monotonic()
        try:
            for record in tick(store, dry_run=dry_run):
                _log(record)
            _beat()
        except Exception as exc:  # a bad tick must not kill the worker
            _log({"event": "worker.tick_failed",
                  "error": "%s: %s" % (type(exc).__name__, exc)})
        # Sleep the remainder of the interval so ticks do not drift.
        stopper.wait(max(0.0, interval - (time.monotonic() - started)))
    _log({"event": "worker.stopped"})


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    config.load_dotenv()
    dry_run = "--dry-run" in argv or config.get_bool("AKSPROFILE_DRY_RUN")
    once = "--once" in argv
    interval = config.get_int("AKSPROFILE_TICK_SECONDS")
    for index, arg in enumerate(argv):
        if arg == "--interval" and index + 1 < len(argv):
            interval = int(argv[index + 1])
    try:
        store = open_store()
    except (ValueError, RuntimeError) as exc:
        # Misconfiguration, not a crash. Say what to set.
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
