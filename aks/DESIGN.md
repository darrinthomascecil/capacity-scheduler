# AKS Scheduled Scaling — Specification

**Status:** draft for implementation
**Source:** extracted from the design discussion previously pasted into this file.

Requirements marked **[S]** are stated in that discussion. Requirements marked
**[I]** are inferred — they were implied but never written down, and each one is
a decision someone should confirm. Nothing here is carried over from any other
project.

---

## 1. What this is

An application that turns a plain-English description of a scaling schedule into
a **named, persistent, indefinitely recurring scaling profile** against an AKS
node pool, and executes it on schedule. **[S]**

The scope is profiles, not one-off commands: *"named, editable scaling profiles
with recurring schedules and no required end date — not just individual
scheduled commands."* **[S]**

### 1.1 Worked example

Input:

```
Cluster:   cluster1
Node pool: apps
Profile:   "Scale to 4 nodes every weekday at 6am, then scale down to 1 at 8pm."
Timezone:  America/Los_Angeles
```

Stored result:

| Setting | Value |
|---|---|
| Target | cluster1 / apps |
| Monday–Friday, 06:00–20:00 | 4 nodes |
| Nights and weekends | 1 node |
| End date | None — repeat until paused, changed, or deleted |

The app reports that interpretation back after saving. **[S]**

---

## 2. Functional requirements

### Profiles

| # | Requirement | |
|---|---|---|
| F1 | A profile is created from natural language plus an explicit cluster, node pool, and IANA timezone. | **[S]** |
| F2 | Profiles are **named** and **editable**. | **[S]** |
| F3 | Profiles recur **indefinitely**. An end date is optional and absent by default: *"repeat until paused, changed, or deleted."* | **[S]** |
| F4 | A profile can be **paused**, **changed**, and **deleted**. | **[S]** |
| F5 | Profiles **survive application restarts** — they are persisted, not held in memory. | **[S]** |
| F6 | The app also supports **immediate** and **one-time** actions, distinct from recurring profiles. | **[S]** |

### Interpretation

| # | Requirement | |
|---|---|---|
| F7 | Natural language is interpreted **once, at profile creation**. | **[S]** |
| F8 | **The worker uses the stored settings without reinterpreting the original language on every execution.** | **[S]** |
| F9 | A valid, unambiguous prompt **activates directly — there is no approval step**. | **[S]** |
| F10 | After saving, the app **reports its interpretation** to the user. This is a report, not a confirmation prompt. | **[S]** |
| F11 | The app asks for clarification **only** when information is missing or genuinely ambiguous. | **[S]** |
| F12 | A schedule with no scale-**down** time is incomplete. The app must ask for it rather than assume one. | **[S]** |

### Targeting

| # | Requirement | |
|---|---|---|
| F13 | Clusters and node pools are **discovered via MCP**; the user does not hand-write resource IDs. | **[S]** |
| F14 | The node pool must be **explicitly targeted**. | **[S]** |
| F15 | Discovery runs at profile-creation time to validate the target exists. | **[I]** |

### Execution

| # | Requirement | |
|---|---|---|
| F16 | Scaling actions execute via the **Azure CLI**. | **[S]** |
| F17 | The app authenticates with **one managed identity**. | **[S]** |
| F18 | The worker evaluates stored profiles on a recurring tick and applies whatever the current time calls for. | **[I]** |

### Counts vs floors

| # | Requirement | |
|---|---|---|
| F19 | For **autoscaled** pools, the words *"minimum four"* / *"minimum one"* set the autoscaler **floor**. Bare *"four nodes"* means an **exact count**. The distinction is carried by the language and must be preserved into the stored profile. | **[S]** |
| F20 | A **system** node pool is subject to a minimum-nodes restriction. | **[S]** |

---

## 3. The profile record

**[I]** — the discussion never gave a schema. This is the minimum that satisfies
F1–F12 and F19.

```json
{
  "name": "apps-business-hours",
  "target": {
    "subscription": "<sub-id>",
    "resourceGroup": "<rg>",
    "cluster": "cluster1",
    "nodePool": "apps"
  },
  "timezone": "America/Los_Angeles",
  "mode": "count",
  "windows": [
    { "days": ["Mon","Tue","Wed","Thu","Fri"],
      "start": "06:00", "end": "20:00", "value": 4 }
  ],
  "otherwise": 1,
  "endDate": null,
  "paused": false,
  "sourceText": "Scale to 4 nodes every weekday at 6am, then scale down to 1 at 8pm.",
  "createdAt": "<iso8601>"
}
```

- `mode` is `"count"` (exact) or `"minimum"` (autoscaler floor) — F19.
- `otherwise` is the value outside every window: the *"nights and weekends"* row.
- `sourceText` is retained for display and editing only. **It is never
  re-interpreted at execution time** — F8.

---

## 4. Ambiguity rules

**[I]** — F11 requires clarification "only when ambiguous" but never defines the
word. These are the decision rules; without them the behaviour is unspecified.

**Ask** when:

1. No scale-down value or time can be determined (F12).
2. No timezone is supplied and none can be inferred.
3. The named cluster or node pool does not appear in discovery, or the name
   matches more than one.
4. A count cannot be resolved to an integer ("a few nodes", "double it").
5. `mode` is undeterminable for an autoscaled pool — the text says neither
   "minimum" nor a bare count.

**Do not ask** — decide and report — when:

6. Days are unstated: default to every day.
7. "Weekday" / "weekend" are used: Mon–Fri and Sat–Sun.
8. A 12-hour time has an unambiguous meaning in context ("6am" → `06:00`).

---

## 5. Time and DST

**[I]** — timezone is a stated field, but its semantics were never given. These
are load-bearing.

- All window boundaries are **wall-clock times in the profile's timezone**.
- The worker compares the current instant, converted to that timezone, against
  the window — it does not precompute UTC instants.
- **Spring forward:** a window boundary that lands in the skipped hour takes
  effect at the first tick after the gap.
- **Fall back:** a window spanning the repeated hour stays in effect across both
  passes; it does not toggle.

---

## 6. Safety

**[I]** — nothing in the discussion bounded what a profile may request. F20
implies bounds exist, so they must be explicit.

- Every target carries an **absolute minimum and maximum**, set by a person, not
  by the language. Every computed value is clamped into that range before any
  Azure CLI call.
- The clamp is what makes a misparse survivable: the worst outcome is a valid
  node count, never zero and never unbounded.
- **System pools** carry a floor of at least 1 and are rejected below it (F20).
- A profile that fails to apply leaves the pool at its previous value and is
  retried on the next tick.

---

## 7. Conflict resolution

**[I]** — two profiles can target one node pool. Unspecified in the discussion.

- Windows within a single profile are evaluated **in order; first match wins**.
- Across profiles on the same target, the **most recently updated active
  profile wins**. Enforced in `worker.select_winners()`, which groups by target
  and applies exactly one profile per pool per tick. Losers are logged with
  `action: superseded` and a reason naming the winner — silently dropping them
  would trade thrashing for a quieter failure: an active profile that does
  nothing, with no explanation.
- `create` also warns on overlap, naming which profile will be superseded.
- Ordering comes from `updatedAt` carried in the profile record, not from
  `store.list()`, whose ordering is a backend implementation detail.

---

## 8. Non-goals

- Not a replacement for the cluster autoscaler. A profile sets counts or floors;
  it does not react to load. **[I]**
- No workload/pod scheduling — node capacity only. **[I]**
- No approval workflow (F9 states the opposite explicitly). **[S]**

---

## 9. Acceptance criteria

1. Creating a profile from the §1.1 example yields exactly the stored record in
   §3 and prints the §1.1 table.
2. `"Scale up to four nodes every weekday at 6am"` — with no scale-down — is
   **rejected with a question**, not saved (F12).
3. Restarting the app preserves every profile, paused state included (F5).
4. The worker applies the correct value at a window boundary **without invoking
   any language model** (F8) — verifiable by running the worker with no model
   credentials configured.
5. `"minimum four"` produces `mode: "minimum"`; `"four nodes"` produces
   `mode: "count"` (F19).
6. A value outside a target's absolute bounds is clamped, and the clamp is
   reported (§6).
7. A profile targeting a pool absent from discovery is rejected (F13/F14).
8. A weekday window resolves correctly on both sides of a DST transition (§5).

---

## 10. Open questions

1. **Which model performs the interpretation in F7?** Never stated.
2. **What MCP server provides discovery in F13?** Named but not identified.
3. ~~**Where do profiles persist?**~~ **Resolved:** Azure Table Storage. ETag
   optimistic concurrency, managed-identity auth, no server to operate. There is
   no local-file fallback — F5 cannot be satisfied by ephemeral disk.
4. **What is the worker's tick interval?** Determines boundary precision.
5. **Who sets the absolute bounds in §6, and where?**
