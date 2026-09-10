# aksprofile

Named, persistent, indefinitely recurring scaling profiles for AKS node pools,
created from plain English.

**Spec:** [DESIGN.md](DESIGN.md) — every requirement is traceable to it.

```bash
export AKSPROFILE_STORE=table://<storage-account>   # required, no default

./aksprofile discover
./aksprofile bounds <cluster> <pool>        # then edit ~/.aksprofile/targets.json
./aksprofile create apps-hours --cluster cluster1 --pool apps \
    "Scale to 4 nodes every weekday at 6am, then scale down to 1 at 8pm."
./aksprofile create apps-hours --cluster cluster1 --pool apps \
    --prompt-file schedule.txt          # or -f -, to read stdin
./aksprofile list | show <name> | pause <name> | resume <name> | delete <name>
./aksprofile now --dry-run                  # one worker tick
python3 -m aksprofile.worker --interval 60  # run the worker
```

## The shape of it

```
  plain English ──► interpret.py ──► profile record ──► store.py (Azure Tables)
                    (once, at                              │
                     creation)                             ▼
                                                        worker.py ──► executor.py ──► az
                                                           │
                                                       model.py  (pure: resolve + clamp)
```

**The worker never re-interprets language.** `worker.py` imports `model`,
`store`, `bounds`, `executor` — and nothing that can reach a model. A test walks
the import graph to keep it that way, so the thing running every 60 seconds is
deterministic no matter what the model did at creation time.

## Where state lives

Profiles persist in **Azure Table Storage** — durable, serverless, pennies a
month at this volume, and ETag optimistic concurrency so a lost update raises
`ConcurrentModification` instead of silently clobbering. Auth is
`DefaultAzureCredential`: a managed identity when deployed, `az login` locally.

```bash
pip install -r requirements.txt
export AKSPROFILE_STORE=table://mystorageaccount
```

There is **no default store**. A store that silently fell back to something
local is how F5 gets violated without anyone noticing, so an unset
`AKSPROFILE_STORE` is an error. The `memory://` backend exists only so the test
suite can run without a storage account; it declares `durable = False` and
cannot satisfy F5.

## One profile per pool

Two active profiles on the same node pool used to both apply on every tick, each
undoing the other. `worker.select_winners()` now groups by target and runs only
the most recently updated one; the rest are logged as `superseded` with the
winner named. `create` warns when it detects the overlap.

## Two things that stop a misparse hurting you

**Absolute bounds** live in `~/.aksprofile/targets.json`, authored by a person.
Every value is clamped into them before any `az` call, so the worst outcome of a
misread sentence is a valid node count. A target with no entry gets a tight
fallback (max 3) rather than free rein.

**Count vs minimum.** "four nodes" sets an exact count; "minimum four" sets the
autoscaler floor. Asking for an exact count on an autoscaled pool — or a minimum
on a pool without the autoscaler — is refused with an explanation, not forced.

## Incomplete prompts are questions, not guesses

```
$ ./aksprofile create x --cluster c --pool p "Scale up to four nodes every weekday at 6am"
That schedule is incomplete. Please answer:
  - What should it scale down to?
```

The rules for what counts as incomplete are in `interpret.to_profile`, in
ordinary code — not left to the model's judgement.

## Layout

```
DESIGN.md                    the specification
aksprofile                 CLI entry point
src/aksprofile/
  model.py                 profile record, resolution, clamping — pure, no I/O
  store.py                 Azure Table Storage persistence
  discovery.py             cluster/node pool lookup (az; swappable for MCP)
  executor.py              applies values via the Azure CLI
  bounds.py                human-authored absolute limits
  worker.py                the tick loop — cannot reach a model
  interpret.py             English -> profile, once, at creation
  cli.py                   commands
tests/test_acceptance.py   one class per acceptance criterion
```

## Running it as a container

```bash
cp .env.example .env            # then fill in AKSPROFILE_STORE
cp targets.example.json targets.json
docker compose up --build
```

The image is built on `mcr.microsoft.com/azure-cli` rather than a slim Python
base, and that is a deliberate, costly choice: the spec requires execution
through the Azure CLI (F16), and `DefaultAzureCredential` shells out to the same
binary. A slim base produces a container that decides correctly and then cannot
act. The price is ~1GB and the CLI's CVE stream. Replacing the subprocess with
ARM REST plus an IMDS token would let this drop back to `python:3.12-slim`.

Three things the container gets right that are easy to miss:

- **tzdata is installed.** Every decision resolves a wall-clock window, and the
  base image raises `ZoneInfoNotFoundError` without it — at the first tick, not
  at startup, so the container would pass its health check and then throw.
- **SIGTERM is handled.** `docker stop` exits 0 in under a second rather than
  being SIGKILLed mid-scale.
- **The app has its own virtualenv** at `/opt/venv`. `az` runs on the system
  Python and bundles its own `azure-core`; sharing them invites version
  collisions between the CLI and the app.

It runs as uid 10001, has no inbound listener, and writes nothing but a
heartbeat file that the `HEALTHCHECK` reads to tell a wedged worker from a
healthy idle one.

## Configuration files

| File | Committed? | Purpose |
|---|---|---|
| `.env.example` | yes | documented template |
| `.env` | **no** | your actual settings |
| `targets.example.json` | yes | bounds template |
| `targets.json` | **no** | your actual guardrails, mounted read-only |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

32 tests, offline — no Azure, no model, no network.

## Configuration

| Variable | Default |
|---|---|
| `AKSPROFILE_STORE` | none — **must** be set to `table://<storage-account>` |
| `AKSPROFILE_BOUNDS` | `~/.aksprofile/targets.json` |
| `AKSPROFILE_MODEL` | `gpt-5.4-mini` |
| `AKSPROFILE_BASE_URL` | `https://api.openai.com/v1` |
| `AKSPROFILE_API_KEY_VAR` | `OPENAI_API_KEY` |

Azure auth is whatever `az` is logged in as — one identity.
