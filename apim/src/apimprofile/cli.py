"""
    apimprofile discover
    apimprofile create <name> "<plain english>"   |  -f <file>  |  -f -
    apimprofile list | show <name> | pause <name> | resume <name> | delete <name>
    apimprofile now [--dry-run]
    apimprofile bounds <service>
    apimprofile durations          observed scale times (DESIGN.md D1)
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
from .model import ProfileError, clamp, describe, resolve, validate
from .store import ConcurrentModification, open_store
from .worker import tick


def _fail(message):
    print("error: %s" % message, file=sys.stderr)
    return 1


def _read_prompt(args):
    if args.prompt_file:
        if args.prompt_file == "-":
            text, source = sys.stdin.read(), "stdin"
        else:
            if not os.path.exists(args.prompt_file):
                raise ValueError("prompt file not found: %s" % args.prompt_file)
            with open(args.prompt_file) as handle:
                text = handle.read()
            source = args.prompt_file
        text = "\n".join(ln for ln in text.splitlines()
                         if not ln.strip().startswith("#")).strip()
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


def cmd_discover(args):
    rows = Discovery().instances()
    if not rows:
        return _fail("no API Management instances visible to this login")
    print("%-28s %-22s %-12s %-6s %-6s %s"
          % ("SERVICE", "RESOURCE GROUP", "TIER", "UNITS", "ZONES", "STATE"))
    for r in rows:
        print("%-28s %-22s %-12s %-6s %-6s %s"
              % (r["service"][:28], r["resourceGroup"][:22], r.get("tier"),
                 r.get("capacity"), r.get("zoneCount") or "-",
                 r.get("provisioningState")))
    return 0


def cmd_create(args):
    from .interpret import NeedsClarification, to_profile

    try:
        args.text = _read_prompt(args)
    except ValueError as exc:
        return _fail(str(exc))

    discovery = Discovery()
    try:
        if args.service:
            instance = discovery.resolve(args.service, args.resource_group)
            inventory = [instance]
        else:
            inventory = discovery.instances()
            if not inventory:
                return _fail("no API Management instances visible to this login")
            instance = None
    except DiscoveryError as exc:
        return _fail(str(exc))

    try:
        if args.from_json:
            with open(args.from_json) as handle:
                proposal = json.load(handle)
        else:
            from .interpret import call_model
            proposal = call_model(args.text, inventory=inventory)

        if instance is None:
            from .interpret import InterpretError, resolve_instance
            try:
                instance = resolve_instance(proposal, inventory)
            except InterpretError as exc:
                return _fail(str(exc))
            if instance is None:
                raise NeedsClarification([
                    "Which API Management instance? Name it in the prompt, or "
                    "pass --service."])

        profile = to_profile(args.name, args.text, instance, proposal,
                             timezone=args.timezone, prewarm=args.prewarm)
        validate(profile)
    except NeedsClarification as exc:
        print("That schedule is incomplete. Please answer:", file=sys.stderr)
        for question in exc.questions:
            print("  - %s" % question, file=sys.stderr)
        return 2
    except ProfileError as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail("%s: %s" % (type(exc).__name__, exc))

    tier = instance.get("tier")
    pool_bounds, defaulted = bounds_mod.for_target(profile["target"])

    if args.dry_run:
        print(describe(profile, tier=tier))
        print("\n(dry run -- not saved)")
        return 0

    with open_store() as store:
        clashes = store.conflicting(profile)
        store.save(profile)

    print(describe(profile, tier=tier))
    if profile.get("notes"):
        print("\nAssumptions: %s" % profile["notes"])
    for value in (profile["units"], profile["baselineUnits"]):
        _, was_clamped, note = clamp(value, pool_bounds, tier=tier,
                                     zone_count=instance.get("zoneCount") or 0)
        if was_clamped:
            print("\nwarning: %s" % note, file=sys.stderr)
    if profile["baselineUnits"] == 1:
        print("\nwarning: scaling down to 1 unit leaves no headroom for platform "
              "maintenance; Azure recommends treating 1-unit instances as a "
              "special case.", file=sys.stderr)
    if defaulted:
        print("\nwarning: no bounds configured for this instance; using the tight "
              "fallback %s. Run `apimprofile bounds %s` to set them."
              % (bounds_mod.FALLBACK, profile["target"]["service"]), file=sys.stderr)
    if clashes:
        print("\nwarning: other active profiles target this instance: %s. APIM "
              "locks during a scale, so they will fight." % ", ".join(clashes),
              file=sys.stderr)
    print("\nProfile %r is active." % profile["name"])
    return 0


def cmd_list(args):
    with open_store() as store:
        profiles = store.list()
    if not profiles:
        print("no profiles")
        return 0
    print("%-22s %-26s %-7s %-9s %-8s %s"
          % ("NAME", "TARGET", "UNITS", "WINDOW", "STATE", "TIMEZONE"))
    for p in profiles:
        print("%-22s %-26s %-7s %-9s %-8s %s"
              % (p["name"][:22], p["target"]["service"][:26],
                 "%s/%s" % (p["units"], p["baselineUnits"]),
                 "%s-%s" % (p["scaleUpAt"], p["scaleDownAt"]),
                 "paused" if p.get("paused") else "active", p["timezone"]))
    return 0


def cmd_show(args):
    with open_store() as store:
        profile = store.get(args.name)
    if not profile:
        return _fail("no profile named %r" % args.name)
    print(describe(profile))
    print("\nOriginal request: %s" % profile.get("sourceText", "(none)"))
    decision = resolve(profile, _dt.datetime.now(_dt.timezone.utc))
    print("Right now:        %s (%s)"
          % ("inactive" if not decision["active"] else "%d units" % decision["units"],
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


def cmd_pause(args): return _set_paused(args.name, True)
def cmd_resume(args): return _set_paused(args.name, False)


def cmd_delete(args):
    with open_store() as store:
        ok = store.delete(args.name)
    if not ok:
        return _fail("no profile named %r" % args.name)
    print("deleted %s" % args.name)
    return 0


def cmd_now(args):
    with open_store() as store:
        for record in tick(store, dry_run=args.dry_run):
            print(json.dumps(record, separators=(",", ":")))
    return 0


def cmd_bounds(args):
    try:
        instance = Discovery().resolve(args.service, args.resource_group)
    except DiscoveryError as exc:
        return _fail(str(exc))
    target = {"subscription": instance["subscription"],
              "resourceGroup": instance["resourceGroup"],
              "service": instance["service"]}
    print("// merge into %s, then SET THESE DELIBERATELY:\n"
          % config.get("APIMPROFILE_BOUNDS"))
    print(json.dumps(bounds_mod.scaffold(target, instance), indent=2))
    return 0


def cmd_durations(args):
    """Observed scale times -- the data behind the pre-warm setting."""
    with open_store() as store:
        seconds = store.scale_durations(200)
    if not seconds:
        print("No completed scales recorded yet. Pre-warm stays at the "
              "conservative default until there is data.")
        return 0
    seconds.sort()
    p95 = seconds[min(len(seconds) - 1, int(len(seconds) * 0.95))]
    print("observed scales: %d" % len(seconds))
    print("  fastest: %dm%02ds" % (seconds[0] // 60, seconds[0] % 60))
    print("  median:  %dm%02ds" % (seconds[len(seconds) // 2] // 60,
                                   seconds[len(seconds) // 2] % 60))
    print("  p95:     %dm%02ds" % (p95 // 60, p95 % 60))
    print("\nPre-warm should be at least the p95. It is not adjusted "
          "automatically -- change it deliberately with --prewarm.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="apimprofile",
                                     description="Scheduled scaling for Azure API Management.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="List APIM instances").set_defaults(func=cmd_discover)

    create = sub.add_parser("create", help="Create a profile from plain English")
    create.add_argument("name")
    create.add_argument("text", nargs="?", default="")
    create.add_argument("--service", help="Overrides any instance named in the prompt")
    create.add_argument("--resource-group")
    create.add_argument("--timezone")
    create.add_argument("--prewarm", type=int,
                        help="Minutes before scaleUpAt to issue the scale (default 45)")
    create.add_argument("--prompt-file", "-f", metavar="PATH",
                        help="Read the schedule from a file ('-' for stdin)")
    create.add_argument("--from-json", help="Apply a proposal from a file (offline testing)")
    create.add_argument("--dry-run", action="store_true")
    create.set_defaults(func=cmd_create)

    sub.add_parser("list").set_defaults(func=cmd_list)

    show = sub.add_parser("show")
    show.add_argument("name")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    for verb, fn in (("pause", cmd_pause), ("resume", cmd_resume), ("delete", cmd_delete)):
        p = sub.add_parser(verb)
        p.add_argument("name")
        p.set_defaults(func=fn)

    now = sub.add_parser("now", help="Run one worker tick")
    now.add_argument("--dry-run", action="store_true")
    now.set_defaults(func=cmd_now)

    b = sub.add_parser("bounds", help="Scaffold an absolute-bounds entry")
    b.add_argument("service")
    b.add_argument("--resource-group")
    b.set_defaults(func=cmd_bounds)

    sub.add_parser("durations", help="Observed scale durations").set_defaults(
        func=cmd_durations)
    return parser


def main(argv=None):
    config.load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        return _fail(str(exc))
    except ConcurrentModification as exc:
        return _fail("%s -- re-read it and try again" % exc)
    except RuntimeError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
