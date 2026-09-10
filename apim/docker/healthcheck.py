#!/usr/bin/env python3
"""Fail if the worker has not completed a tick recently."""
import os, sys, time

path = os.environ.get("APIMPROFILE_HEARTBEAT", "/tmp/apimprofile.heartbeat")
limit = int(os.environ.get("APIMPROFILE_TICK_SECONDS", "300")) * 2 + 60

if not os.path.exists(path):
    print("no heartbeat at %s" % path, file=sys.stderr); sys.exit(1)
age = time.time() - os.path.getmtime(path)
if age > limit:
    print("heartbeat is %.0fs old (limit %ds)" % (age, limit), file=sys.stderr); sys.exit(1)
print("ok (%.0fs)" % age)
