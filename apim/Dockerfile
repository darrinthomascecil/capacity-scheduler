# apimprofile -- the scheduling worker.
#
# Built on the Azure CLI image because scaling goes through `az rest` and
# DefaultAzureCredential shells out to the same binary. A slim Python base makes
# a container that decides correctly and then cannot act.
FROM mcr.microsoft.com/azure-cli:latest AS base

# This base is Azure Linux 3.0: tdnf, no pip, no useradd, and -- verified -- no
# tzdata. Every scheduling decision resolves a wall-clock window, and the
# failure without tzdata appears at the first tick, not at startup.
RUN tdnf install -y tzdata python3-pip shadow-utils && tdnf clean all

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    APIMPROFILE_HEARTBEAT=/tmp/apimprofile.heartbeat

WORKDIR /app

# The app gets its own virtualenv: `az` runs on the system Python and bundles
# its own azure-core, so sharing them invites version collisions.
RUN python3 -m venv /opt/venv
COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
COPY docker/healthcheck.py /app/healthcheck.py

RUN useradd --create-home --uid 10001 apimprofile
USER 10001

HEALTHCHECK --interval=120s --timeout=5s --start-period=60s --retries=3 \
  CMD ["/opt/venv/bin/python", "/app/healthcheck.py"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/venv/bin/python", "-m", "apimprofile.worker"]
