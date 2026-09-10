#!/usr/bin/env python3
"""Fail if the worker has not completed a tick recently.

Exit 0 = healthy. Anything else marks the container unhealthy.
"""
import os
import sys
import time

path = os.environ.get("AKSPROFILE_HEARTBEAT", "/tmp/aksprofile.heartbeat")
# Two missed ticks is unhealthy; one may just be a slow ARM call.
limit = int(os.environ.get("AKSPROFILE_TICK_SECONDS", "60")) * 2 + 30

if not os.path.exists(path):
    print("no heartbeat at %s" % path, file=sys.stderr)
    sys.exit(1)

age = time.time() - os.path.getmtime(path)
if age > limit:
    print("heartbeat is %.0fs old (limit %ds)" % (age, limit), file=sys.stderr)
    sys.exit(1)
print("ok (%.0fs)" % age)
