# capacity-scheduler

Scheduled capacity for Azure, described in plain English. Two schedulers,
same shape, different execution models.

| | | |
|---|---|---|
| [**aks/**](aks/) | AKS node pools | node count or autoscaler floor |
| [**apim/**](apim/) | API Management | scale units |

Each is a self-contained app: its own design doc, source, tests, container, and
`.env`. Nothing is shared but the pattern — and a `.venv` at the root, which
both entry points find automatically.

## The pattern

```
plain English ──► interpret.py ──► profile ──► store.py (Azure Tables)
                  (once, at                       │
                   creation)                      ▼
                                              worker.py ──► executor.py ──► az
                                                  │
                                              model.py  (pure: resolve + clamp)
```

Three properties both apps hold, and the tests enforce:

**The worker never re-interprets language.** It imports `model`, `store`,
`bounds`, `executor` — and nothing that can reach a model. Each app has a test
that walks the import graph to keep it that way, so the thing running on a timer
is deterministic no matter what the model decided at creation.

**Guardrails are human-authored.** `targets.json` holds absolute limits the
interpreter can never write. Every value is clamped before any ARM call, so the
worst outcome of a misread sentence is a valid number.

**Incomplete input is a question, not a guess.** A schedule with no scale-down
is refused, because guessing there means paying for peak capacity overnight.

## Where they diverge

The execution models are different enough that sharing code would have been
worse than sharing the shape:

| | AKS | APIM |
|---|---|---|
| Scale time | ~2 min | **15–45 min** (BasicV2: under a minute, measured) |
| Tick | 60s | **300s** — precision finer than the operation buys nothing |
| Pre-warm | 45 min | 45 min, and load-bearing rather than a tunable |
| Mid-operation | retry next tick | **the service locks**; retrying just generates failures |
| Terminal action | `applied` | **`issued`** — a 202 means started, not finished |
| Scale duration | known | **measured by the scheduler itself** |
| Modes | `count` / `minimum` | units only — no floor concept exists |

Both fill a real gap: Azure's built-in autoscale for AKS and APIM is metric-driven
only, so a schedule is the right tool for load that is known in advance.

[apim/DESIGN.md §3](apim/DESIGN.md) explains why copying the AKS executor would
have produced something that looks correct and behaves badly.

## Getting started

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r aks/requirements.txt

cd aks   && cp .env.example .env && ./aksprofile  --help
cd apim  && cp .env.example .env && ./apimprofile --help
```

Both require a `*PROFILE_STORE=table://<storage-account>` — there is
deliberately no default, since a store that silently falls back to local disk is
how "profiles survive restarts" gets violated without anyone noticing.

## Layout

Both have the same shape:

```
<app>/
  DESIGN.md               the design and its reasoning
  README.md               how to use it
  <app>profile            entry point (finds ../.venv)
  Dockerfile              Azure CLI base; tzdata, non-root, SIGTERM, healthcheck
  compose.yaml
  .env.example            documented; .env is gitignored
  targets.example.json    guardrail template
  requirements.txt        only the Azure SDK, only for the table backend
  src/<app>profile/
    model.py              pure: record, resolution, clamping. No I/O.
    store.py              Azure Table Storage + an in-memory backend for tests
    discovery.py          inventory, so a prompt can only name what exists
    executor.py           applies values via az
    bounds.py             human-authored absolute limits
    interpret.py          English -> profile, once
    worker.py             the tick loop — cannot reach interpret.py
    cli.py
    config.py             .env loader
  docker/healthcheck.py
  tests/
```

`aks/` additionally has [TESTING.md](aks/TESTING.md), a six-level guide from
offline tests through to changing real node counts.
