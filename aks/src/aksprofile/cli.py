"""
Command line interface.

    aksprofile discover
    aksprofile create <name> --cluster C --pool P --timezone TZ "<plain english>"
    aksprofile list | show <name> | pause <name> | resume <name> | delete <name>
    aksprofile now [--dry-run]        run one worker tick
    aksprofile apply <name> <value>   immediate one-off action (F6)
    aksprofile bounds <cluster> <pool>  scaffold a bounds entry
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

from . import bounds as bounds_mod
from . import config
from .discovery import Discovery, DiscoveryError
from .executor import ExecutionError, apply as apply_value, observe
from .model import ProfileError, clamp, describe, resolve, validate
from .store import ConcurrentModification, open_store
from .worker import tick


def _fail(message):
    print("error: %s" % message, file=sys.stderr)
    return 1


def cmd_discover(args):
    d = Discovery()
    clusters = d.clusters()
    if not clusters:
        return _fail("no AKS clusters visible to this login")
    for cluster in clusters:
        print("%s  (rg %s, %s, %s)" % (cluster["name"], cluster["resourceGroup"],
                                       cluster["location"], cluster.get("powerState")))
        for pool in d.node_pools(cluster["name"], cluster["resourceGroup"]):
            print("    %-16s mode=%-7s count=%-4s min=%-4s max=%-4s autoscale=%s"
                  % (pool["name"], pool["mode"], pool["count"],
                     "-" if pool["min"] is None else pool["min"],
                     "-" if pool["max"] is None else pool["max"],
                     "on" if pool["autoscale"] else "off"))
    return 0


def _read_prompt(args):
    """The schedule text, from --prompt-file, the positional argument, or stdin.

    A file keeps a long schedule readable and reviewable in version control
    instead of fighting shell quoting. `-` means stdin, so the prompt can be
    piped from anything.
    """
    if args.prompt_file:
        if args.prompt_file == "-":
            text = sys.stdin.read()
            source = "stdin"
        else:
            if not os.path.exists(args.prompt_file):
                raise ValueError("prompt file not found: %s" % args.prompt_file)
            with open(args.prompt_file) as handle:
                text = handle.read()
            source = args.prompt_file
        # Comments and blank lines, so a prompt file can explain itself.
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
        text = "\n".join(lines).strip()
        if not text:
            raise ValueError("%s contains no prompt text" % source)
        return text

    if args.text:
        return args.text

    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            return text

    raise ValueError("give me the schedule: as an argument, with --prompt-file, "
                     "or on stdin")


def cmd_create(args):
    from .interpret import NeedsClarification, to_profile  # imported only here

    try:
        args.text = _read_prompt(args)
    except ValueError as exc:
        return _fail(str(exc))

    discovery = Discovery()
    found = None
    inventory = []
    try:
        if args.cluster and args.pool:
            # Explicit flags: no need to enumerate everything.
            found = discovery.resolve_target(args.cluster, args.pool)
        else:
            # The prompt names the pool. Give the model the real inventory so it
            # can only pick something that exists (F13, F14).
            inventory = discovery.inventory()
            if not inventory:
                return _fail("no AKS node pools visible to this login")
    except DiscoveryError as exc:
        return _fail(str(exc))

    try:
        if args.from_json:
            # Offline path: apply a proposal from a file instead of calling the
            # model. Exercises everything downstream of the model call.
            with open(args.from_json) as handle:
                proposal = json.load(handle)
        else:
            from .interpret import call_model
            proposal = call_model(args.text, inventory=inventory)

        if found is None:
            from .interpret import InterpretError, resolve_target_from
            try:
                target, pool_row = resolve_target_from(proposal, inventory)
            except InterpretError as exc:
                return _fail(str(exc))
            if target is None:
                raise NeedsClarification([
                    "Which cluster and node pool? Name it in the prompt, or pass "
                    "--cluster and --pool."])
            found = {"target": target,
                     "pool": {"mode": pool_row.get("mode"),
                              "count": pool_row.get("count")}}

        profile = to_profile(args.name, args.text, found["target"],
                             proposal, timezone=args.timezone)
    except NeedsClarification as exc:
        print("That schedule is incomplete. Please answer:", file=sys.stderr)
        for question in exc.questions:
            print("  - %s" % question, file=sys.stderr)
        return 2
    except Exception as exc:
        return _fail("%s: %s" % (type(exc).__name__, exc))

    try:
        validate(profile)
    except ProfileError as exc:
        return _fail("the interpreted schedule is invalid: %s" % exc)

    pool = found["pool"]
    pool_bounds, defaulted = bounds_mod.for_target(profile["target"])
    is_system = (pool.get("mode") or "").lower() == "system"

    if args.dry_run:
        print(describe(profile))
        print("\n(dry run -- not saved)")
        return 0

    with open_store() as store:
        clashes = store.conflicting(profile)
        store.save(profile)

    # F10: report the interpretation. Not a prompt -- it is already active (F9).
    print(describe(profile))
    if profile.get("notes"):
        print("\nAssumptions: %s" % profile["notes"])

    for value in [w["value"] for w in profile["windows"]] + [profile["otherwise"]]:
        _, was_clamped, note = clamp(value, pool_bounds, is_system)
        if was_clamped:
            print("\nwarning: %s" % note, file=sys.stderr)
    if defaulted:
        print("\nwarning: no bounds configured for this pool; using the tight fallback "
              "%s. Run `aksprofile bounds %s %s` to set them."
              % (bounds_mod.FALLBACK, args.cluster, args.pool), file=sys.stderr)
    if clashes:
        # This profile was just saved, so it is the most recently updated and
        # therefore the one that will run. Say that, rather than leaving the
        # reader to work out which of them wins.
        print("\nwarning: %s also target%s this pool and will be SUPERSEDED by "
              "%r on every tick. Pause or delete %s if that is not what you want."
              % (", ".join(repr(c) for c in clashes),
                 "s" if len(clashes) == 1 else "",
                 profile["name"],
                 "it" if len(clashes) == 1 else "them"),
              file=sys.stderr)
    print("\nProfile %r is active." % profile["name"])
    return 0


def cmd_list(args):
    with open_store() as store:
        profiles = store.list()
    if not profiles:
        print("no profiles")
        return 0
    print("%-24s %-26s %-9s %-8s %s" % ("NAME", "TARGET", "MODE", "STATE", "TIMEZONE"))
    for p in profiles:
        print("%-24s %-26s %-9s %-8s %s"
              % (p["name"][:24],
                 ("%s/%s" % (p["target"]["cluster"], p["target"]["nodePool"]))[:26],
                 p["mode"], "paused" if p.get("paused") else "active", p["timezone"]))
    return 0


def cmd_show(args):
    with open_store() as store:
        profile = store.get(args.name)
    if not profile:
        return _fail("no profile named %r" % args.name)
    print(describe(profile))
    print("\nOriginal request: %s" % profile.get("sourceText", "(none)"))
    decision = resolve(profile, _dt.datetime.now(_dt.timezone.utc))
    print("Right now:        %s (%s)" % (
        "inactive" if not decision["active"] else "%d %s" % (
            decision["value"], "nodes" if profile["mode"] == "count" else "minimum nodes"),
        decision["reason"]))
    if args.json:
        print("\n" + json.dumps(profile, indent=2))
    return 0


def _set_paused(name, paused):
    with open_store() as store:
        try:
            store.set_paused(name, paused)
        except ProfileError as exc:
            return _fail(str(exc))
    print("%s %s" % (name, "paused" if paused else "resumed"))
    return 0


def cmd_pause(args):
    return _set_paused(args.name, True)


def cmd_resume(args):
    return _set_paused(args.name, False)


def cmd_delete(args):
    with open_store() as store:
        ok = store.delete(args.name)
    if not ok:
        return _fail("no profile named %r" % args.name)
    print("deleted %s" % args.name)
    return 0


def cmd_now(args):
    with open_store() as store:
        records = tick(store, dry_run=args.dry_run)
    for record in records:
        print(json.dumps(record, separators=(",", ":")))
    return 0


def cmd_apply(args):
    """Immediate, one-time action (F6). Bypasses profiles; still clamped."""
    try:
        found = Discovery().resolve_target(args.cluster, args.pool)
    except DiscoveryError as exc:
        return _fail(str(exc))
    target = found["target"]
    pool_bounds, defaulted = bounds_mod.for_target(target)
    is_system = (found["pool"].get("mode") or "").lower() == "system"
    value, was_clamped, note = clamp(args.value, pool_bounds, is_system)
    if was_clamped:
        print("warning: %s" % note, file=sys.stderr)
    if defaulted:
        print("warning: no bounds configured; using fallback %s"
              % bounds_mod.FALLBACK, file=sys.stderr)
    try:
        result = apply_value(target, value, args.mode, dry_run=args.dry_run)
    except ExecutionError as exc:
        return _fail(str(exc))
    print(json.dumps({"target": "%s/%s" % (target["cluster"], target["nodePool"]),
                      "value": value, "mode": args.mode,
                      "changed": result["changed"], "skipped": result["skipped"],
                      "command": result["command"]}, indent=2))
    return 0


def cmd_bounds(args):
    try:
        found = Discovery().resolve_target(args.cluster, args.pool)
    except DiscoveryError as exc:
        return _fail(str(exc))
    entry = bounds_mod.scaffold(found["target"], found["pool"])
    print("// merge into %s, then SET THESE DELIBERATELY:\n" % bounds_mod.DEFAULT_PATH)
    print(json.dumps(entry, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="aksprofile",
                                     description="Named, recurring AKS scaling profiles.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="List clusters and node pools").set_defaults(func=cmd_discover)

    create = sub.add_parser("create", help="Create a profile from plain English")
    create.add_argument("name")
    create.add_argument("text", nargs="?", default="",
                        help="The schedule, in plain English")
    create.add_argument("--cluster", help="Overrides any cluster named in the prompt")
    create.add_argument("--pool", help="Overrides any node pool named in the prompt")
    create.add_argument("--timezone", help="IANA name; overrides anything in the text")
    create.add_argument("--prompt-file", "-f", metavar="PATH",
                        help="Read the schedule from a file ('-' for stdin). "
                             "Lines starting with # are ignored.")
    create.add_argument("--from-json", help="Apply a proposal from this file instead of "
                                            "calling the model (offline testing)")
    create.add_argument("--dry-run", action="store_true",
                        help="Show the interpretation without saving")
    create.set_defaults(func=cmd_create)

    sub.add_parser("list", help="List profiles").set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="Show one profile and what it wants right now")
    show.add_argument("name")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    for verb, fn, helptext in (("pause", cmd_pause, "Pause a profile"),
                               ("resume", cmd_resume, "Resume a profile"),
                               ("delete", cmd_delete, "Delete a profile")):
        p = sub.add_parser(verb, help=helptext)
        p.add_argument("name")
        p.set_defaults(func=fn)

    now = sub.add_parser("now", help="Run one worker tick immediately")
    now.add_argument("--dry-run", action="store_true")
    now.set_defaults(func=cmd_now)

    ap = sub.add_parser("apply", help="Immediate one-time scale (F6)")
    ap.add_argument("cluster")
    ap.add_argument("pool")
    ap.add_argument("value", type=int)
    ap.add_argument("--mode", choices=["count", "minimum"], default="count")
    ap.add_argument("--dry-run", action="store_true")
    ap.set_defaults(func=cmd_apply)

    b = sub.add_parser("bounds", help="Scaffold an absolute-bounds entry")
    b.add_argument("cluster")
    b.add_argument("pool")
    b.set_defaults(func=cmd_bounds)

    return parser


def main(argv=None):
    config.load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        # Most commonly: AKSPROFILE_STORE is unset. There is deliberately no
        # default store, so say what to set rather than dumping a traceback.
        return _fail(str(exc))
    except ConcurrentModification as exc:
        return _fail("%s -- re-read it and try again" % exc)
    except RuntimeError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
