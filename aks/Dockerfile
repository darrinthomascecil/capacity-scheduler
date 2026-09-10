# aksprofile — the scheduling worker.
#
# Deliberately plain: a Python base, the Azure SDK, and the source. No build
# stage, no compilation, nothing to cache-bust.

# The Azure CLI is a hard dependency, not a convenience: the spec requires
# execution through it (F16), and DefaultAzureCredential's AzureCliCredential
# shells out to the same binary. A slim Python base produces a container that
# decides correctly and then cannot act -- so the CLI image is the honest base.
#
# The cost is real: ~700MB and its own CVE stream. Replacing the subprocess with
# ARM REST plus an IMDS token would let this go back to python:3.12-slim.
FROM mcr.microsoft.com/azure-cli:latest AS base

# `zoneinfo` is load-bearing: every scheduling decision resolves a wall-clock
# window in the profile's timezone. A base without the IANA database fails at
# the first tick, not at startup -- so the container would pass its health check
# and then throw on the first real decision.
# Verified: this base raises ZoneInfoNotFoundError without tzdata, and ships no
# pip at all.
RUN tdnf install -y tzdata python3-pip shadow-utils && tdnf clean all

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    AKSPROFILE_HEARTBEAT=/tmp/aksprofile.heartbeat

WORKDIR /app

# The app gets its own virtualenv. `az` runs on the system Python and bundles
# its own azure-core; installing our SDK there would risk version collisions
# between the CLI and the app.
RUN python3 -m venv /opt/venv
COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/

# Nothing here needs root, and there is no inbound listener and no writable state.
RUN useradd --create-home --uid 10001 aksprofile
USER 10001

# A wedged worker must be distinguishable from a healthy idle one. The worker
# touches the heartbeat every tick; this fails it if the file goes stale.
COPY --chown=10001:10001 docker/healthcheck.py /app/healthcheck.py
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/opt/venv/bin/python", "/app/healthcheck.py"]

# SIGTERM is handled: the worker finishes its slice and exits cleanly.
STOPSIGNAL SIGTERM

ENTRYPOINT ["/opt/venv/bin/python", "-m", "aksprofile.worker"]
