# Testing aksprofile

Six levels, cheapest first. Each one is independently useful — stop wherever you
have the confidence you need.

Every output below is real, copied from an actual run against
`$CLUSTER` on 2026-09-09.

---

## Set your parameters

Every command below uses these. Set them once per shell.

```bash
export CLUSTER=my-aks-cluster            # AKS cluster name
export RESOURCE_GROUP=my-resource-group  # the cluster's resource group
export POOL=my-nodepool                  # node pool to schedule
export PROFILE=business-hours            # a name for the profile you create
export STORAGE_ACCOUNT=mystorageaccount  # where profiles are stored

export AKSPROFILE_STORE=table://$STORAGE_ACCOUNT
cd /path/to/this/repo
```

`./aksprofile discover` (level 2) lists real cluster and pool names if you are
not sure what to put here.

### One-time setup

| | |
|---|---|
| **Store** | An Azure Storage account. The tables (`aksprofiles`, `aksprofileruns`) are created on first use. |
| **RBAC** | Grant yourself **Storage Table Data Contributor** on that account — control-plane access does not imply data-plane access. See [Troubleshooting](#troubleshooting). |
| **Bounds** | `targets.json` — absolute per-pool limits. `./aksprofile bounds $CLUSTER $POOL` scaffolds an entry from current state. |
| **Interpretation** | `AKSPROFILE_BASE_URL` in `.env`. Point it at an Azure AI endpoint and no API key is needed. |
| **venv** | `python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt` |

The example outputs below came from a real run; your cluster and pool names will
differ.

---

## Level 1 — Offline tests

No Azure, no SDK, no network, no credentials.

```bash
python3 -m unittest discover -s tests -v
```

```
Ran 56 tests in 0.033s
OK
```

Eight classes named `AC1_`…`AC8_` map one-to-one to the acceptance criteria in
[DESIGN.md](DESIGN.md) §9. The rest cover the store contract, ETag concurrency, and
backend selection.

**Worth running specifically** — the test that proves the worker can't reach a
language model, by walking the import graph rather than trusting a comment:

```bash
python3 -m unittest tests.test_acceptance.AC4_WorkerNeedsNoModel -v
```

---

## Level 2 — Read-only against Azure

```bash
./aksprofile discover
```

```
my-aks-cluster  (rg my-resource-group, eastus, Running)
    my-nodepool      mode=User    count=1    min=-    max=-    autoscale=off
```

```bash
./aksprofile list
./aksprofile show $PROFILE
```

`show` prints the stored interpretation plus what it wants *right now*:

```
Target: my-aks-cluster / my-nodepool
Monday-Friday, 06:00-20:00: 4 nodes
Otherwise: 1 nodes
End date: None - repeat until paused, changed, or deleted
Timezone: America/Los_Angeles

Original request: Scale to 4 nodes every weekday at 6am, then scale down to 1 at 8pm.
Right now:        4 nodes (Wed 06:00-20:00 America/Los_Angeles)
```

---

## Level 3 — The whole pipeline, without a model

`--from-json` substitutes a canned interpretation, so everything downstream of
the model call is exercised offline.

```bash
cat > /tmp/proposal.json <<'JSON'
{ "windows":[{"days":["Mon","Tue","Wed","Thu","Fri"],"start":"06:00","end":"20:00","value":4}],
  "otherwise":1, "mode":"count", "timezone":"America/Los_Angeles",
  "missing":[], "notes":"Assumed Pacific time." }
JSON

./aksprofile create $PROFILE --cluster $CLUSTER --pool $POOL \
  --from-json /tmp/proposal.json --dry-run
```

### The refusals are the interesting part

**Incomplete schedule** — set `"otherwise": null` and `"missing": ["scale_down_value"]`:

```
That schedule is incomplete. Please answer:
  - What should it scale down to?
```
exit 2. Nothing saved. This is F12 — a missing scale-down is a question, not a guess.

**Unknown pool:**

```bash
./aksprofile create x --cluster $CLUSTER --pool batch --from-json /tmp/proposal.json
```
```
error: cluster 'my-aks-cluster' has no node pool named 'batch'. Available: my-nodepool
```

**No store configured:**

```bash
env -u AKSPROFILE_STORE ./aksprofile list
```
```
error: no store configured. Set AKSPROFILE_STORE=table://<storage-account>
```

There is deliberately no default store — a silent fallback to local disk is how
"profiles survive restarts" gets violated without anyone noticing.

---

### Passing the prompt from a file

Long schedules are easier to read, review and version-control in a file than
wedged into shell quoting.

```bash
./aksprofile create $PROFILE --cluster $CLUSTER --pool $POOL \
  --prompt-file schedule.example.txt --dry-run
```

`-f` is the short form. `-f -` reads stdin, and a piped stdin is used
automatically even without the flag:

```bash
echo "two nodes weekdays 10am to 4pm london time, one otherwise" \
  | ./aksprofile create x --cluster $CLUSTER --pool $POOL --dry-run
```

Lines starting with `#` are ignored, so a prompt file can explain itself. See
[schedule.example.txt](schedule.example.txt).

---

## Level 4 — Real Table Storage

**Persistence across processes** — write in one, read in another:

```bash
./.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from aksprofile.store import open_store
with open_store() as s:
    p, etag = s.get_with_etag('$PROFILE')
    print(p['name'], p['windows'][0]['value'], p['otherwise'])
    print('etag:', etag)
"
```

```
business-hours 4 1
etag: W/"datetime'2026-09-09T15%3A49%3A13.3161663Z'"
```

**ETag concurrency** — a stale write must lose, not clobber:

```bash
./.venv/bin/python -c "
import sys; sys.path.insert(0,'src')
from aksprofile.store import open_store, ConcurrentModification
with open_store() as s:
    p, etag = s.get_with_etag('$PROFILE')
    other = dict(p); other['otherwise'] = 2; s.save(other)   # someone else writes
    mine = dict(p); mine['otherwise'] = 3
    try: s.save(mine, etag=etag); print('BUG: not rejected')
    except ConcurrentModification as e: print('rejected:', e)
    print('server value:', s.get('$PROFILE')['otherwise'])
"
```

```
rejected: '$PROFILE' changed since it was read
server value: 2
```

---

## Level 5 — The container

```bash
rm -rf /tmp/azcfg && cp -R ~/.azure /tmp/azcfg && chmod -R a+rwX /tmp/azcfg
docker compose up --build
```

Or a single tick:

```bash
docker build -t aksprofile:local .
docker run --rm \
  -e AKSPROFILE_STORE=table://$STORAGE_ACCOUNT \
  -e AKSPROFILE_BOUNDS=/etc/aksprofile/targets.json \
  -e AKSPROFILE_DRY_RUN=true \
  -e AZURE_CONFIG_DIR=/home/aksprofile/.azure \
  -v /tmp/azcfg:/home/aksprofile/.azure \
  -v "$PWD/targets.json:/etc/aksprofile/targets.json:ro" \
  aksprofile:local --once
```

```json
{"ts":"2026-09-09T15:59:35Z","profile":"$PROFILE","target":"my-aks-cluster/my-nodepool",
 "mode":"count","desired":2,"action":"skipped","clamped":true,"dryRun":true,
 "reason":"Wed 06:00-20:00 America/Los_Angeles; clamped 4 -> 2 (bounds 1..2, system pool); dry run"}
```

### Things worth checking in the image

```bash
docker run --rm --entrypoint sh aksprofile:local -c '
  az version --query "\"azure-cli\"" -o tsv
  /opt/venv/bin/python -c "from zoneinfo import ZoneInfo; import datetime as dt;
    print(dt.datetime(2026,9,9,12,tzinfo=ZoneInfo(\"America/Los_Angeles\")).strftime(\"%Z\"),
          dt.datetime(2026,1,9,12,tzinfo=ZoneInfo(\"America/Los_Angeles\")).strftime(\"%Z\"))"
  id -u -n
'
```

```
2.90.0
PDT PST          ← tzdata present and DST-aware
aksprofile       ← non-root, uid 10001
```

The timezone check matters: the base image ships **without** tzdata, and the
failure appears at the first scheduling decision rather than at startup — so a
container missing it passes its health check and then throws.

### Graceful shutdown

```bash
CID=$(docker run -d -e AKSPROFILE_STORE=table://$STORAGE_ACCOUNT \
  -e AKSPROFILE_DRY_RUN=true -e AKSPROFILE_TICK_SECONDS=300 \
  -e AZURE_CONFIG_DIR=/home/aksprofile/.azure \
  -v /tmp/azcfg:/home/aksprofile/.azure aksprofile:local)
sleep 30
docker inspect "$CID" --format '{{.State.Health.Status}}'      # healthy
docker stop -t 30 "$CID"
docker inspect "$CID" --format '{{.State.ExitCode}}'           # 0, not 137
docker logs "$CID" | tail -2
docker rm "$CID"
```

```
{"event":"worker.shutdown","signal":15}
{"event":"worker.stopped"}
```

Exit 0 in under a second. `137` would mean it was SIGKILLed mid-scale.

---

## Level 6 — Actually change the node count

**This one costs money and changes real infrastructure.**

```bash
az aks show -n $CLUSTER -g $RESOURCE_GROUP --query powerState.code -o tsv
# must be "Running" — if "Stopped": az aks start -n $CLUSTER -g $RESOURCE_GROUP
```

Record the starting point:

```bash
az aks nodepool show --cluster-name $CLUSTER -g $RESOURCE_GROUP \
  --name $POOL --query count -o tsv        # 1
```

Dry run first — always:

```bash
./aksprofile now --dry-run
```

Then for real:

```bash
./aksprofile now
```

Wait a couple of minutes, then confirm:

```bash
az aks nodepool show --cluster-name $CLUSTER -g $RESOURCE_GROUP \
  --name $POOL --query count -o tsv        # 2
```

**It goes to 2, not 4.** `targets.json` caps that pool at `absoluteMax: 2`, and
the log says so: `clamped 4 -> 2 (bounds 1..2, system pool)`. That clamp is the
thing standing between a misread sentence and a runaway cluster. To see the full
4, raise the bound deliberately.

### Independent confirmation — the activity log

Don't take the app's word for it:

```bash
CLUSTER="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.ContainerService/managedClusters/$CLUSTER"
az monitor activity-log list --resource-id "$CLUSTER" --offset 1h -o json \
  | python3 -c "
import sys,json
for r in json.load(sys.stdin):
    op=(r.get('operationName') or {}).get('localizedValue','')
    if 'Agent Pool' in op and (r.get('status') or {}).get('value')=='Succeeded':
        print(r['eventTimestamp'][:19], op, '|', r.get('caller'))
"
```

A successful run appears as `Create or Update Agent Pool`. As of this writing
the cluster has had **four** such writes, all from earlier testing of a
different project — `aksprofile` has never written to it.

### Put it back

```bash
./aksprofile pause $PROFILE
./aksprofile apply $CLUSTER $POOL 1
az aks stop -n $CLUSTER -g $RESOURCE_GROUP       # stops node billing
```

---

## Troubleshooting

Every one of these was hit for real while building this.

**`AuthorizationPermissionMismatch` on the store.** Control-plane access to a
storage account does not grant data-plane access. You need **Storage Table Data
Contributor** on the account, and it takes a minute or two to propagate:

```bash
az role assignment create --assignee-object-id <your-object-id> \
  --assignee-principal-type User --role "Storage Table Data Contributor" \
  --scope $(az storage account show -n $STORAGE_ACCOUNT --query id -o tsv)
```

**`DefaultAzureCredential failed` inside the container.** `AzureCliCredential`
shells out to `az`, which needs a **writable** config directory — a `:ro` mount
of `~/.azure` fails when it refreshes its session. Mount a copy read-write.
In production this disappears entirely; a managed identity needs no mount.

**`the table backend needs pip install -r requirements.txt`.** The Azure SDK is
imported lazily so the memory backend and the whole test suite need nothing
installed. Use `./.venv/bin/python`, or the `./aksprofile` wrapper, which
prefers the venv automatically.

**`'minimum' requires the cluster autoscaler, which is disabled on this pool`.**
Correct behaviour, not a bug. a System-mode pool with autoscaling off, so a floor can't be
set — use `mode: count`. The reverse is also refused: an exact count on an
autoscaled pool. Neither is silently forced.

**The worker did nothing and said `dryRun: true`.** Check `AKSPROFILE_DRY_RUN`
in `.env`. It ships `true` on purpose — a new deployment should observe before
it enforces.

---

## What is still untested

`aksprofile create` against a live model. It needs `OPENAI_API_KEY`:

```bash
export OPENAI_API_KEY=sk-...
./aksprofile create $PROFILE --cluster $CLUSTER --pool $POOL \
  --timezone America/Los_Angeles --dry-run \
  "Scale to 4 nodes every weekday at 6am, then scale down to 1 at 8pm."
```

Three prompts worth trying:

| Prompt | Expected |
|---|---|
| the full sentence above | matches the [DESIGN.md](DESIGN.md) §1.1 table exactly |
| `"Scale up to four nodes every weekday at 6am"` | **asks** for the scale-down time |
| `"minimum four on weekdays, minimum one otherwise"` | `mode: minimum` |

Everything after the model call is already covered by level 3.
