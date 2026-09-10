# APIM Scheduled Scaling — Design

**Status:** design, not implemented
**Scope:** one sentence naming an APIM instance, a unit count, a scale-up time
and a scale-down time, turned into a persistent daily scaling profile.

    "Scale my-apim to 4 units at 9am Central and back down at 6pm."

Deliberately narrow. Multi-region, multiple windows per day, and per-window unit
counts are out (§10) — they can be added later without changing this shape.

Companion to the AKS scheduler in [`../aks/DESIGN.md`](../aks/DESIGN.md). The shape is
deliberately the same — plain English in, a stored profile, a worker that never
re-interprets language. **What APIM changes is the execution model**, and that
change is large enough that copying the AKS executor would produce something
that looks correct and behaves badly.

Sources are Microsoft Learn, cited inline. *(The Azure docs MCP plugin failed
with a .NET assembly load error, so this was researched against Learn directly.)*

---

## 1. What scaling means for APIM

An APIM instance scales by **units**. A unit is a fixed bundle of compute with
an estimated throughput; you add and remove whole units.
([upgrade-and-scale](https://learn.microsoft.com/en-us/azure/api-management/upgrade-and-scale))

There are two distinct levers, and only the first is in scope:

| Lever | What it is | In scope |
|---|---|---|
| **`sku.capacity`** | number of units in a tier | **yes** |
| **`sku.name`** | the tier itself (Basic → Premium) | **no** — see §10 |

### 1.1 Not every tier can be scheduled

| Tier | Max units | Schedulable |
|---|---|---|
| **Developer** | 1 | **No** — cannot add units, no SLA, and scaling to/from it causes downtime |
| **Consumption** | n/a | **No** — scales itself on traffic; cannot be manually scaled |
| Basic | per pricing page | yes |
| Standard | 4 | yes |
| **Basic v2** | **10** | yes |
| **Standard v2** | **10** | yes |
| **Premium** | no fixed limit | yes, plus per-region units |
| **Premium v2** | **30** | yes |

Developer and Consumption must be **refused at profile creation**, not skipped
at run time. A schedule against a tier that cannot scale is a mistake worth
surfacing while someone is still looking at the screen.

---

## 2. The ARM contract

```http
PATCH https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}
      /providers/Microsoft.ApiManagement/service/{name}?api-version=2024-05-01
```

```json
{ "sku": { "name": "Premium", "capacity": 3 } }
```

Response: **`202 Accepted`**, with `Location` and `Azure-AsyncOperation`
headers.
([REST: service update](https://learn.microsoft.com/en-us/rest/api/apimanagement/api-management-service/update))

Two properties matter for a scheduler:

- `properties.provisioningState` — current state
- `properties.targetProvisioningState` — *the state a long-running operation is
  driving toward.* Non-empty means an operation is in flight.

Multi-region instances carry `properties.additionalLocations[]`, each with its
own `sku.capacity`. The top-level `sku.capacity` is the **primary** region only.

---

## 3. What makes this different from AKS

This section is the design. Everything else follows from it.

### 3.1 A scale takes 15–45 minutes, not 2–3

> "Changes to your API Management service's infrastructure … can take **15
> minutes or longer** … Expect longer times for an instance with a greater
> number of scale units or multi-region configuration."
> — [upgrade-and-scale](https://learn.microsoft.com/en-us/azure/api-management/upgrade-and-scale)

> "Scaling operation can take **around 30 minutes**, so you should plan your
> rules accordingly."
> — [api-management-capacity](https://learn.microsoft.com/en-us/azure/api-management/api-management-capacity)

**Consequence.** Pre-warm is not optional and is not minutes — it is
**tens of minutes**. A profile that says "scale up at 9am" must issue the write
around 08:15 to have capacity at 09:00. The AKS scheduler treats prewarm as a
tunable; here it is load-bearing, and a schedule with a window shorter than the
scale time is incoherent and should be rejected.

**A window must be at least `2 × expected scale duration`** — long enough to
scale up, be useful, and scale down. Default that to 90 minutes and let it be
overridden per target.

### 3.2 The service locks while it changes

> "While the service is updating, **other service infrastructure changes can't
> be made.**"

> "If the service is locked by another operation, the scaling request will fail
> and retry automatically."
> — [api-management-howto-autoscale](https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-autoscale)

**Consequence.** The AKS pattern — re-evaluate every 60s and re-issue — is
actively harmful here. Issuing a second PATCH into a locked service produces a
failure, and a scheduler that retries every minute for 30 minutes generates 30
failures and a lot of noise for one legitimate operation.

The worker must therefore be **in-flight aware**:

1. Read `targetProvisioningState` before deciding anything.
2. If it is non-empty, the instance is mid-operation → **log `inflight` and do
   nothing.** Not an error, not a retry.
3. Only when `provisioningState` is `Succeeded` and `targetProvisioningState`
   is empty may a write be issued.

This is the same shape as the AKS `provisioningState` precondition, but where
that one guards a 2-minute window this guards a 45-minute one, and it is the
difference between a quiet log and a hundred spurious errors per scale.

### 3.3 Tick interval must be much slower

AKS runs at 60s because a node arrives in ~2 minutes. For APIM a 60-second tick
buys nothing — nothing can change that fast — and costs an ARM read every
minute per instance.

**Default to 5 minutes.** Boundary precision of ±5 minutes is irrelevant against
an operation that takes 30.

### 3.4 One unit is a special case

> "If your instance … is configured with only **1 unit**, upgrade or scale it
> when a capacity metric value exceeds **40%** … to reserve capacity for guest
> OS updates."

Relevant here because a scheduled scale-**down** to 1 unit leaves no headroom
for platform maintenance. A profile whose off-peak value is 1 should warn.

### 3.5 Availability zones constrain the number

> "If you select specific zones, the number of API Management units in autoscale
> rules and limits must be a **multiple of the number of zones** configured."

**Consequence.** For a zone-pinned instance, valid unit counts are multiples of
the zone count. A profile asking for 5 units on a 3-zone instance is invalid and
must be rejected at creation, not discovered at 6am.

### 3.6 Multi-region is a per-location schedule

Premium instances hold units per region. Azure Monitor autoscale can only act on
the **primary** location; other regions are manual — which is precisely the gap a
scheduler fills.

**Out of scope here** (§10), but it constrains what the ARM write may touch: a
PATCH of top-level `sku.capacity` affects the **primary region only** and leaves
`additionalLocations[]` alone. That is the correct behaviour for this scope — but
it means a Premium instance with three regions will only have one of them
scheduled, and anyone reading a bill should know that.

---

## 4. Where this fits against autoscale

Azure already offers metric-based autoscale via
`Microsoft.Insights/autoscalesettings`, with recommended rules of +1 unit above
70% capacity over 30 minutes (60-minute cooldown) and −1 unit below 35%
(90-minute cooldown).
([autoscale](https://learn.microsoft.com/en-us/azure/api-management/api-management-howto-autoscale))

**This scheduler is not a replacement for that, and the two must not both drive
the same instance.** Metric autoscale reacts *after* load arrives — and with a
30-minute averaging window plus a 30-minute scale, that is an hour late for a
predictable 9am spike. Scheduling is the right tool when the load is *known in
advance*; autoscale is right when it is not.

If both are configured, they will fight — one scaling in on low overnight
capacity while the other scales out for a scheduled morning. **Detect an
existing autoscale setting on the target at profile creation and refuse, or
require an explicit override.**

---

## 5. The profile record

Four things come from the prompt: **service, units, up time, down time.**
Everything else is either discovered or human-authored.

```json
{
  "name": "apim-business-hours",
  "target": {
    "subscription": "<sub-id>",
    "resourceGroup": "<rg>",
    "service": "my-apim"
  },
  "timezone": "America/Chicago",
  "days": ["Mon","Tue","Wed","Thu","Fri"],
  "scaleUpAt": "09:00",
  "scaleDownAt": "18:00",
  "units": 4,
  "baselineUnits": 2,
  "prewarmMinutes": 45,
  "endDate": null,
  "paused": false,
  "sourceText": "Scale my-apim to 4 units at 9am Central and back down at 6pm.",
  "createdAt": "<iso8601>"
}
```

| Field | Source |
|---|---|
| `service` | **prompt**, matched against discovered inventory |
| `units` | **prompt** — the scaled-up count |
| `scaleUpAt` / `scaleDownAt` | **prompt** — wall-clock in `timezone` |
| `timezone` | prompt, or asked for if absent |
| `days` | prompt, or every day if unstated |
| `baselineUnits` | **captured from the instance at creation** — see below |
| `prewarmMinutes` | human-authored per target, from measured scale duration (§3.1) |

### 5.1 What it scales back down *to*

The prompt says "back down" without saying to what. Rather than guess or force a
fifth input, **`baselineUnits` is read off the instance at profile creation** and
recorded. Scaling back down returns it to exactly what it was before the profile
existed.

This is the safe default: the worst case is the instance ends up where it
started. It is reported explicitly after saving — *"back down to 2 units (its
capacity now)"* — so a wrong assumption is visible immediately rather than at
6pm. If the prompt does name a down value, that wins.

### 5.2 Resolution

Given an instant, in `timezone`:

- inside `[scaleUpAt − prewarmMinutes, scaleDownAt)` on a matching day → `units`
- otherwise → `baselineUnits`

One window, first-match-is-the-only-match. There is no window ordering problem
because there is only one.

## 6. Natural language → profile

Identical architecture to the AKS tool: **interpret once at creation, store the
result, never re-interpret at execution.**

### 6.1 What the model may set

| May set | May never set |
|---|---|
| `service` — chosen from discovered inventory | `subscription`, `resourceGroup` |
| `units` | `baselineUnits` (read from the instance) |
| `scaleUpAt`, `scaleDownAt` | `absoluteMin` / `absoluteMax` |
| `timezone`, `days` | `sku.name` (the tier) |
| | `prewarmMinutes`, `paused`, `endDate` |

The model picks the instance from a **discovered inventory** of real APIM
services — name, tier, current capacity, zone count — so it can only name
something that exists, and the unit count can be validated against that tier's
limit before anything is stored.

### 6.2 Ambiguity rules

**Ask** when:

1. No scale-**down time** is given. "Scale to 4 units at 9am" is incomplete —
   the same rule the AKS scheduler enforces, and for the same reason: a missing
   scale-down means paying for peak capacity overnight.
2. No unit count can be resolved to an integer.
3. No timezone is supplied or inferable.
4. The named service does not exist, or matches more than one.

**Refuse outright** — the request cannot be satisfied, so a question is pointless:

5. The target tier is **Developer** or **Consumption** (§1.1).
6. The requested units exceed the tier maximum.
7. The units are not a multiple of the zone count on a zone-pinned instance (§3.5).
8. `scaleDownAt − scaleUpAt` is shorter than `2 × expected scale duration` (§3.1).

**Decide and report** when days are unstated (every day), or "weekday" /
"weekend" is used, or a 12-hour time is unambiguous in context.

### 6.3 Worked example

```
"Scale my-apim to 4 units at 9am Central and back down at 6pm on weekdays."
```

```
Target:     my-apim  (Premium, currently 2 units)
Mon-Fri:    4 units from 09:00 to 18:00 America/Chicago
Otherwise:  2 units  (its capacity now)
Pre-warm:   45 minutes -- the scale is issued at 08:15
End date:   None - repeat until paused, changed, or deleted
```

## 7. Safety

- **Absolute per-target min and max units**, human-authored, never model-set.
  Every value is clamped before any PATCH.
- **Tier maximum is a hard ceiling** independent of the configured bounds — a
  clamp cannot exceed what the tier allows.
- **Never change `sku.name`.** The scheduler adjusts capacity within a tier.
  Downgrading a tier can silently remove VNet integration or multi-region
  ([upgrade-and-scale](https://learn.microsoft.com/en-us/azure/api-management/upgrade-and-scale)),
  which is not something a schedule should ever do at 6am.
- **A failed PATCH leaves the instance alone** and is retried on the next
  eligible tick — but only once the in-flight check clears (§3.2).
- **Scale-down is rate-limited; scale-up is not.** A wrong shrink degrades
  service; a wrong growth costs money. Only one of those pages someone.

---

## 8. Cost is the point, and it is per-unit-hour

APIM units are billed hourly, so the savings arithmetic is the same shape as
AKS node-hours. A Premium instance held at 4 units around the clock versus 4
during a 9-hour weekday window and 2 otherwise:

| | Unit-hours / week |
|---|---|
| Flat at 4 | 4 × 168 = **672** |
| Scheduled | (5 × 9 × 4) + ((168 − 45) × 2) = 180 + 246 = **426** |

**≈ 37% reduction.** Lower than the AKS example because the pre-warm and the
long scale time mean fewer usable hours at the low value — which is exactly why
§3.1 matters commercially and not just operationally.

---

## 9. Acceptance criteria

1. `"Scale my-apim to 4 units at 9am Central and back down at 6pm"` produces the
   §6.3 record, with `baselineUnits` read from the live instance.
2. The same sentence **without** "and back down at 6pm" is refused with a
   question, not saved.
3. A profile against a **Developer** or **Consumption** instance is refused at
   creation, with the tier named in the reason.
4. Units above the tier maximum are refused; units above the configured bound
   are clamped and reported.
5. On a zone-pinned instance, a unit count that is not a multiple of the zone
   count is refused.
6. A window shorter than `2 × expected scale duration` is refused at creation.
7. With `targetProvisioningState` non-empty, the worker logs `inflight` and
   issues **no** PATCH.
8. `prewarmMinutes` shifts the effective start earlier while the stored
   `scaleUpAt` still records the business time the user asked for.
9. The worker applies stored settings with **no model credentials configured**.
10. `sku.name` is never written by any code path — provable by grep and by test.

## 10. Non-goals

- **Changing tiers.** Upgrade/downgrade is a human decision with feature
  consequences (§7).
- **Replacing metric autoscale** (§4).
- **Consumption tier** anything — it scales itself.
- **Scaling workspace gateways.** They have their own units and metrics and are
  a separate target type, additive later.
- **Multi-region.** A Premium instance holds units per region (§3.6), but this
  scope targets the primary location only. Adding `location` to the target is a
  later change and does not alter the profile shape.
- **More than one window a day.** One up time and one down time. A second window
  would need the ordered-windows model from the AKS scheduler; nothing here
  precludes adding it.

---

## 11. Decisions on the open questions

### D1 — The scheduler measures its own scale duration

The docs say 15–45 minutes and "it depends," so no one can supply this number
accurately up front. But the scheduler is uniquely placed to *measure* it: it
knows when it issued a PATCH, and it sees `targetProvisioningState` go empty on
a later tick.

- **Start at `prewarmMinutes: 45`.** The asymmetry decides the default — too much
  pre-warm costs money, too little causes a bad open.
- **Record `scaleDurationSeconds` on every completed scale**, with the from/to
  unit counts, in the run history.
- **Report observed durations; never self-adjust.** A scheduler that silently
  retunes its own timing is much harder to reason about at 6am. Surface it —
  *"last 8 scale-ups took 22–31 minutes; prewarm is 45"* — and let a person
  tighten it.
- The minimum-window rule (§3.1) uses measured data once there is any, and
  `2 × 45 = 90` minutes until then.

### D2 — Observe state, do not poll the async operation

Follows from D1. A 5-minute tick measures duration to ±5 minutes, which is ample
for setting a 45-minute pre-warm. Polling `Azure-AsyncOperation` would buy
precision with no use for it, and would cost a **stateful** worker that has to
carry operation URLs across restarts. Observe `targetProvisioningState`; stay
stateless.

### D3 — Still genuinely open: draining on scale-down

Docs promise no gateway downtime outside Developer, but *"no downtime"* and
*"no dropped connections"* are different claims and the second is not made.
Does not block building; **must** be tested under load before a scheduled
shrink runs against production.

### D4 — Capacity metrics are reporting, never control

A schedule-driven system never reads them to decide. Their value is answering
"was 4 units actually right?" after the fact — if observed capacity sat at 20%
all week, the schedule is over-provisioned. That is the report that justifies
the project's existence, and it is the only reason to wire them in.

### D5 — Multi-region ordering: deferred with the feature

Moot at this scope (primary region only, §10). Returns with multi-region, and
the likely answer is that per-region locks serialise, so an N-region profile
needs roughly N× the pre-warm. Confirm before building it.
