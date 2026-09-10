# apimprofile

Scheduled scaling for Azure API Management, from a sentence.

**Design and its reasoning: [DESIGN.md](DESIGN.md)** — read §3 first, it is the
part that differs from the AKS scheduler.

```bash
./apimprofile create business-hours \
  "Scale my-apim to 4 units at 9am Central and back down at 6pm on weekdays"
```

```
Target:     my-apim  (Premium, currently 2 units)
Mon-Fri:    4 units from 09:00 to 18:00 America/Chicago
Otherwise:  2 units  (its capacity when this profile was created)
Pre-warm:   45 minutes -- the scale is issued at 08:15
End date:   None - repeat until paused, changed, or deleted
```

Four things come from the prompt: **instance, units, up time, down time.**

## Why this is not just the AKS scheduler with names changed

**A scale takes 15–45 minutes, not 2–3.** Pre-warm stops being a tunable and
becomes the point: a 9am scale-up is issued at 08:15. A window shorter than
about 90 minutes cannot fit a scale up and a scale down, and is refused.

**The service locks while it changes.** Re-issuing every tick — correct for AKS
— produces a failure here, so the worker reads `targetProvisioningState` and
treats in-flight as `inflight`: not an error, not a retry.

**The tick is 5 minutes, not 60 seconds.** Precision finer than the operation
itself buys nothing and costs an ARM read a minute.

**It measures its own scale duration.** The docs say "15–45 minutes, it
depends," so nobody can supply the number up front. The worker records how long
each scale actually took and `apimprofile durations` reports the spread. It
never self-adjusts — a scheduler that silently retunes its own timing is much
harder to reason about at 6am.

## Things it refuses outright

Not questions — the request cannot be satisfied:

| | |
|---|---|
| **Developer / Consumption tier** | cannot be scaled at all |
| **Units above the tier maximum** | Standard 4, Basic v2 / Standard v2 10, Premium v2 30 |
| **Units not a multiple of the zone count** | on a zone-pinned instance |
| **A window under ~90 minutes** | a scale cannot complete in it |
| **No scale-down time** | means paying for peak capacity overnight |

It also never writes `sku.name`. Changing tier can silently remove VNet
integration or multi-region, which is not something a schedule does at 6am.

## Commands

```bash
./apimprofile discover                    # instances, tiers, units, zones
./apimprofile bounds <service>            # scaffold absolute limits
./apimprofile create <name> "<english>"   # or -f <file>, or -f - for stdin
./apimprofile list | show <name>
./apimprofile pause <name> | resume <name> | delete <name>
./apimprofile now [--dry-run]             # one tick
./apimprofile durations                   # observed scale times
python3 -m apimprofile.worker             # the loop (needs PYTHONPATH=src)
```

## Container

```bash
cp .env.example .env && cp targets.example.json targets.json
docker compose up --build
```

Same shape as the AKS image and for the same reasons: Azure CLI base (the app
scales through `az rest` and `DefaultAzureCredential` uses the same binary),
`tzdata` installed explicitly, the app in its own virtualenv away from the CLI's
bundled `azure-core`, non-root, SIGTERM handled, heartbeat-based healthcheck.
`stop_grace_period` is 60s because a tick can be waiting on a long operation.

## Configuration

| Variable | Default |
|---|---|
| `APIMPROFILE_STORE` | none — **must** be `table://<storage-account>` |
| `APIMPROFILE_BOUNDS` | `~/.apimprofile/targets.json` |
| `APIMPROFILE_TICK_SECONDS` | `300` |
| `APIMPROFILE_DRY_RUN` | `false` (`.env.example` ships `true`) |
| `APIMPROFILE_BASE_URL` | point at an Azure AI endpoint and no API key is needed |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

35 tests, offline — no Azure, no model, no network. Classes are named for the
acceptance criteria in [DESIGN.md §9](DESIGN.md).

## Not yet exercised against a live instance

There is no APIM instance in this subscription to test against, so `discover`,
the ARM PATCH, and the in-flight path are covered by tests and by construction
but have never run for real. Everything else — interpretation, refusals,
resolution, clamping, the container — has.
