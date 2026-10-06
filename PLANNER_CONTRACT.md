# Portable calendar mirror planner 1.3.1 - JSON contract v3

`planner.py` is a self-contained Python 3.10+ standard-library program. It accepts
normalized JSON and produces a deterministic safe plan. It has no network calls,
credentials, authentication, system-clock reads, calendar writes, scheduler,
registry persistence, or daemon. All authenticated reads/writes remain in the
existing Google Calendar plugin. The adapter and write executor are external.

RuntimeConfig is required input: exactly two explicitly allowed calendars,
verified self identities for each, and a namespace. No deployment accounts,
namespaces, Space/project IDs, credential discovery or ambient defaults are built
into the executable. [RUNTIME_CONFIG.md](RUNTIME_CONFIG.md) defines configuration,
schema migration, state/ledger binding and byte-compatible marker hashing.

Managed creation and serialized_runner retain their existing safety contract;
atomic claims remain optional for capable backends. See
[MANAGED_COVERAGE.md](MANAGED_COVERAGE.md). All packaged examples and fixtures are
synthetic. Real runtime config/state/journal belong outside this distribution.
The optional evidence-bound zero-call recovery and preflight command are specified
in [RECOVERY_CONTRACT.md](RECOVERY_CONTRACT.md). Every executor command must pass
its exit-status and result checks before any subsequent journal or Calendar step.

## Run and transfer

```sh
python3 -m unittest -v test_planner
python3 planner.py examples/create.input.json -o plan.json
python3 planner.py --revalidate examples/revalidate.input.json
```

Or import `plan(data)`, `revalidate_action(action, fresh)` and
`snapshot_fingerprint(snapshot)`. These functions are pure. Input is not mutated.
The CLI reads UTF-8 files or stdin (`-`) and writes a JSON file or stdout. Use a
UTF-8 console for non-ASCII piping on Windows. No other writes are performed.
Duplicate JSON keys, nonfinite numbers, malformed input and incomplete batches
fail closed. Normal CLI exit codes: 0 ready/bootstrap_ready, 1 review_required,
2 blocked. Revalidation exits 0 only when allowed, otherwise 2.

The release contains source, tests, this contract, synthetic examples, test results
and a checksum manifest. Clone the authorized source repository at a verified
commit and verify `planner.py` SHA-256 before loading. No dependency on the
development desktop or a mutable download link exists.

## Fixed policy and unchanged marker compatibility

Only the two exact calendar IDs in validated RuntimeConfig are permitted.
No personal/default account fallback or primary alias is accepted. The namespace
is taken only from the validated config. Event content cannot expand that scope.

One owned mirror is planned per eligible source occurrence on the other calendar,
even if a native busy event overlaps. Native manual `blocked` events are never
adopted/updated/deleted. They may be ordinary busy sources for distinct mirrors.
Exact sync mirrors of any namespace and suspected malformed sync markers are
excluded as sources. ALL registered destination IDs are excluded even if their
markers were damaged or removed. Registry loops are rejected.

The interval is `[actual run_started_at, run_started_at + 90 elapsed days)` plus
ongoing events. End is exclusive. Cancelled, self-declined and transparent events
are excluded. Default/null transparency and opaque events are busy; tentative
status and tentative/needsAction attendance remain busy.

```python
payload = ["dot-block-sync", 1, config["namespace"],
           source_calendar_id, source_event_id,
           source_original_start_time_or_null, destination_calendar_id]
hex_digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')).hexdigest()
marker = '[dot-block-sync:v1:' + config['namespace'] + ':' + hex_digest + ']'
```

Source ID is the returned occurrence/instance ID, never the series ID. Original
start is the exact raw RFC3339 string, raw all-day YYYY-MM-DD string, or JSON null,
not a dateTime object. Never hash current start/end or normalize the original
string. Known IDs always retain the registry raw string/hash/destination ID.
Equivalent connector timestamp formatting is reported and uses the registry
string. Changed instants, null/string changes, series changes, or two IDs claiming
one recurring occurrence are conflicts. Lost raw identities cannot be recovered
from a hash; remain blocked if evidence is insufficient.

Description normalization removes only ASCII U+0009..000D and U+0020. Ownership
requires an anchored whole match to the exact namespace and 64 lowercase hex.
Extra text, multiple markers, Unicode whitespace and uppercase/malformed hashes
are conflicts, never adoption evidence. A substring search is candidate discovery
only. Matching malformed descriptions stop the affected item; distinct IDs with
the same marker stop that source. Repeated identical observations of one ID are
deduplicated; contradictory observations block the entire batch.

## Input envelope and capabilities

All named fields are required. Unknown envelope/record keys are rejected.

```text
{
  schema_version: 3,
  config: RuntimeConfig,
  run_started_at: explicit-offset RFC3339 actual run start,
  connector_capabilities: Capabilities,
  calendars: [Calendar(A), Calendar(B)],
  details: Details,
  state: State
}
```

For the current direct Google Calendar plugin use exactly:

```json
{
  "concurrency_mode": "snapshot_reread",
  "provider_etag": false,
  "conditional_writes": false,
  "field_profile": "google_calendar_projection"
}
```

`etag` is explicitly null in every Event. Do not invent an etag or substitute a
fingerprint into it. The planner produces fingerprints separately. A declared
`provider_etag: true` requires a real nonempty token in every Event. Optional
`concurrency_mode: "provider_etag"` requires both capability booleans true and
provides an actual destination If-Match value for update/delete. A connector that
exposes etags but cannot condition writes can use snapshot_reread with
provider_etag true and conditional_writes false. Contradictions fail closed.

`field_profile: "full"` is available only when the caller can actually observe the
full normalized protected projection. The production plugin uses the explicit
projection profile below. Neither mode supplies a lock. Snapshot fingerprints
detect changes visible in rereads; **without provider CAS they cannot eliminate
a change between reread and write**. Even destination CAS is not a cross-event
transaction or atomic create-if-marker-absent operation. External writer
serialization, post-write verification and unknown-result recovery remain required.

## Calendar inventory: finite scopes, explicit certificates

```text
Calendar = {
  id: allowed calendar ID,
  listing: {
    complete: true, next_page_token: null, expanded: true,
    includes_ongoing: true,
    time_min: actual run start, time_max: run start plus 90 elapsed days,
    events: [Event, ...]
  },
  marker_searches: [MarkerSearch, ...]
}

MarkerSearch = {
  marker: exact canonical marker for this destination calendar,
  scope: "exact_marker_all_destinations",
  complete: boolean,
  next_page_token: null or unread page token,
  events: [full normalized candidate Event, ...]
}
```

The listing certificate means every page of `structuredContent.events` was read
through the final `next_page_token`, recurrence was expanded within the finite
90-day interval, and ongoing events were included. Do not remove free/declined
events before normalization. A missing page or false certificate blocks the whole
plan. Do not reuse a search-only projection as full details.

The baseline ownership inventory consists of **both complete current-window
listings plus every persisted registered destination ID individually re-read**,
including IDs now outside the window or with erased markers. It does NOT require
all-time expansion of native recurrences. `state.mappings_complete: true`
certifies that no persisted owned mapping was omitted; details accounting checks
every source and destination ID. Existing mapped noops/updates/deletes and proven
legacy bootstrap can operate with `marker_searches: []`. Duplicates discovered
within these observations always conflict.

For the UNIVERSAL mode of NEW CREATE or adoption/reconstruction of an UNREGISTERED mirror, supply a
complete per-marker lookup on the destination calendar. Its coverage certificate
means all candidate events for that exact marker, including occurrences outside
the current interval, were accounted for by an actually adequate lookup. It does
not mean all native events were expanded. Read the returned candidates in detail;
the planner checks their entire ASCII-normalized descriptions itself. Include
suspected matching descriptions with extra text as candidates, not just exact
matches. A complete result requires `next_page_token: null`.

An exhausted broad-text search does not automatically prove exhaustive marker
coverage (for example, it might miss a marker containing ASCII whitespace).
**Do not label an indexed query as universal coverage.** If universal coverage
is unavailable, use the explicit managed mode and all its ledger/fencing
requirements, or omit the search/set complete false and block that create.
Unregistered adoption still requires explicit reconciliation. Independent
registered items can still be planned. No blanket global all-time native scan is
required or implied. Known source disappearance still never implies deletion.

The output `expected.marker_inventory` uses scope
`current_window_plus_registered_ids`, includes the exact window, complete
registered destination-ID list and registry completeness flag, any per-marker
lookup certificate/candidates, and sorted matching/suspect observations. The
pre-write guard fingerprints this complete object; the executor must rebuild it
from fresh observations, not copy the stale object and merely assert freshness.

## Event: explicit observable projection

```text
Event = {
  id: nonempty event/instance ID,
  etag: real nonempty token | null according to capabilities,
  status: "confirmed" | "tentative" | "cancelled",
  original_start_time: unchanged raw RFC3339/date string | null,
  recurring_event_id: raw series ID | null,
  start: Time, end: Time,
  all_day_bounds: null | {start: resolved RFC3339 boundary, end: resolved boundary},
  self_response: "none" | "accepted" | "declined" | "tentative" | "needsAction",
  fields_verified: true,
  fields: {
    summary: string, description: string,
    visibility: null | "default" | "public" | "private" | "confidential",
    transparency: null | "opaque" | "transparent",
    eventType: observed string | {"unobserved":true},
    attendees: [{is_self:boolean, response_status: response except "none", ...}, ...],
    location: string | null,
    conferenceData: {"unobserved":true},
    reminders: normalized object,
    hangout_link: returned string | null,
    recurrence: [],
    other: {observed: {other exposed writable fields}, unobserved_provider_fields:true}
  }
}
```

Under google_calendar_projection, `fields_verified: true` certifies that ALL
fields actually exposed by the detailed projection were read and correctly
normalized. It does not promise observation of hidden provider fields. Named
observable fields cannot be omitted or guessed. `conferenceData` must be the
explicit unobserved sentinel, never invented null. `hangout_link` is the observable
conference signal and is protected. An empty link can normalize to null only
according to the detailed connector's known empty-value semantics.

If event_type is returned, keep it as an observed string; do not discard it. An
observed owned mirror must be default. If it is not exposed, use the unobserved
sentinel. A transition from a previously observed protected value to unknown is
a baseline mismatch, not permission to forget it.

`other.observed` preserves every additionally EXPOSED writable field (such as color
or attachments when returned) not represented above. The explicit true flag says
the remainder of provider properties is unobservable, rather than absent. Do not
drop an exposed manual property or invent hidden conferenceData/extendedProperties/
guest permissions. Only verified service defaults may be normalized away. Every
observed protected value is compared against the baseline. Unexposed values cannot
be protected by comparison; write operations must preserve unspecified fields.

In full profile, conferenceData is an observed object/null, eventType must be an
observed string, and `other` is the complete observed extra-field dictionary.

### Mapping the described connector shape

| Connector observation | Normalized value |
| --- | --- |
| Search `structuredContent.events`, `next_page_token` | Consume all pages; listing certificate only afterward |
| Flat `start`, `end` RFC3339 strings | `{dateTime: raw, timeZone: verified zone if supplied}` |
| Flat date-only start/end | `{date: raw, timeZone: verified effective calendar/event zone}` plus resolved all-day bounds |
| Flat `original_start_time` | Exact raw string/null; preserve registry raw form for known identities |
| `recurring_event_id` | Keep series ID; do not replace instance `id` |
| Detailed title/description/status/transparency/visibility/location | Map to named fields using actual returned values |
| Search `my_response_status` | Selection hint only; reconcile with detailed self-attendee result |
| Detailed attendees `is_self: null` | Normalize null to false using the connector's documented projection semantics |
| Detailed attendees `response_status` | Keep raw enum and derive self_response from is_self true entry |
| Detailed reminders `use_default` | Rename to `useDefault`; normalize confirmed empty overrides to [] |
| Detailed `hangout_link` | Preserve as protected observable conference signal |
| Detailed `recurrence` | Must verify empty/null for expanded occurrence; normalize to [] |
| Missing provider etag/CAS | Declare capabilities false; etag null |
| Unexposed provider attributes | Explicit unknown sentinel/remainder; never assume absent |

The read tool describes full event DETAILS but is still a projection. Required
observable fields must be certified by detailed reads; search omissions are not
evidence of absence. A sparse/error detail response fails closed. No live calendars
were queried by this package; exact response values/default semantics remain the
adapter's responsibility. Current create/update tool schemas were inspected:
they expose event_type, add_google_meet, reminders.use_default, and separate timed
or all-day boundaries, but no etag/precondition parameter.

### Time representation

Timed: `{"dateTime":"2026-10-07T18:00:00+09:00","timeZone":"Asia/Tokyo"}`.
Offset is required; timeZone is optional when not supplied. Exact strings are
preserved. All-day: `{"date":"2026-11-01","timeZone":"America/New_York"}`.
Effective timezone is required and end date is exclusive. The adapter resolves
start/end midnight independently into all_day_bounds, e.g. Nov 1 -04:00 and Nov 2
-05:00 for a 25-hour day. The planner verifies increasing instants/date/midnight
consistency but trusts timezone/offset resolution. No timezone database is needed.
Never send all_day_bounds to the Calendar API. Strict timestamps use uppercase
T/Z, seconds and at most six fractional digits; leap seconds and unknown -00:00
offsets are rejected.

## Exact detail-ID accounting

```text
Details = {
  complete: true,
  requested: [{calendar_id, event_id}, ...],
  responses: [{calendar_id, event_id, outcome, event, evidence}, ...]
}
```

Exactly one response is required per requested ID, with no extras. Every
registered source AND destination ID must be requested each run, even if listed.
Partial/missing responses or contradictions with listing snapshots block the
whole batch. Detail results may return events outside the listing interval.

| outcome | event | evidence |
| --- | --- | --- |
| found | full normalized Event | null |
| cancelled | null | explicit known-ID terminal proof |
| deleted | null | explicit known-ID terminal proof |
| not_found | null | null; ambiguous absence is a conflict |
| error | null | null; read/permission failure is a conflict |

Terminal proof example:
`{"kind":"deleted","verified_known_id":true,"proof":"opaque-evidence-reference"}`.
Use cancelled consistently for cancellation. The proof is an adapter assertion
bound to the exact requested ID; a 404 alone, permission failure or missing search
page is not proof of deletion. Sparse cancellation tombstones belong here rather
than in incomplete Events.

## Verified state and safe legacy bootstrap

```text
State = {
  config_fingerprint: fingerprint of the canonical RuntimeConfig,
  status: "verified" | "absent" | "uncertain" | "bootstrap",
  generation: nonempty opaque revision label,
  mappings_complete: boolean,
  mappings: [VerifiedMapping or LegacyMapping, ...]
}

VerifiedMapping = {
  source: {calendar_id, event_id, original_start_time},
  marker: exact canonical marker,
  destination: {calendar_id, event_id},
  verified_source: Event,
  verified_destination: Event
}

LegacyMapping = {
  source: {calendar_id, event_id, original_start_time},
  marker: exact existing marker,
  destination: {calendar_id, event_id},
  legacy: {
    verified: true,
    start: stored normalized Time, end: stored normalized Time,
    status: stored source status,
    self_response: stored source self response,
    evidence: nonempty reference to prior registry verification
  }
}
```

Verified/bootstrap state must certify mappings_complete true. Never mark a partial
or lost registry complete to enable creates. Absent/uncertain state suppresses
every mutation; recovery candidates are only suggestions and require certified
marker lookup, exact ownership and canonical observable fields/timing. Uncertain
write outcomes must be reconciled from the journal and reads before state becomes
verified. A missing/cancelled registered destination is always a conflict, never
permission to blindly recreate it.

Legacy mappings are accepted only in explicit bootstrap state; that state contains
legacy rows only. It does not require nonexistent historical full snapshots. For
EACH existing verified identity/marker/destination ID, require direct source and
destination readbacks, exact stored start/end agreement on both events, unchanged
stored source status/self response, raw original identity agreement (equivalent
formatting preserves the legacy raw hash), native busy source, and all currently
observable canonical mirror constraints. Duplicates, damaged markers, changed
times/status, manual protected changes or missing evidence fail closed. Already
owned registered IDs are being reverified, not adopted as unknown mirrors, so a
new per-marker lookup is not required for this bootstrap path.

When every row passes and no conflict remains, output status bootstrap_ready,
bootstrap_complete true, 0 calendar mutations, and bootstrap_mappings containing
the unchanged identities/hashes/destination IDs plus **new current readbacks**.
These are not invented historical snapshots. The caller can, under external
pause/drain and after reviewing all results, persist them as verified mappings
with a new generation. Run again: matching pairs yield noops. Ordinary unregistered
native events are deferred during bootstrap; they do not require create lookups
until a subsequent verified-state run. Unregistered owned/suspect mirrors still
conflict and are never ignored. Partial bootstrap must not be promoted as a
complete registry. The synthetic 279-row bootstrap test
uses 277 raw original strings and two nulls, no etags and no all-time native scan;
it does not establish correctness of the live production rows.

generation is a stale-plan label, not a CAS/lock/atomic owner claim. Existing
writer pause/drain or a real externally enforced single-writer mechanism is still
required. No registry/scheduler writes are implemented here.

## Output actions and mandatory pre-write revalidation

Normal output contains schema_version, namespace from config, config_fingerprint, status, run_started_at, window,
state_generation, actions, counts, plan_id, concurrency, executor_requirements,
reconstruction_candidates, bootstrap_mappings and bootstrap_complete. Invalid
input produces a global blocked conflict and zero mutations. review_required may
contain independent safe items; blocked/bootstrap_ready never authorize calendar
mutation. Noop/conflict never authorize writes.

Actions have op create/update/delete/noop/conflict, reason, marker, complete
expected source/destination observations, desired projection for create/update,
notes, write_contract, concurrency notice, and deterministic id. expected contains
config, state_config_fingerprint, state_generation, connector_capabilities, source, destination, marker_inventory,
and fingerprints for config and those four observations. Managed creates additionally carry
managed_context and its fingerprint; see the companion contract. Fingerprints are SHA-256 of UTF-8
JSON with sorted object keys, compact separators and ensure_ascii false. They
retain all normalized observable values and explicit unknowns; array order must
be stably normalized by the adapter. They are not provider version tokens.

Use the following fresh input immediately before every write, AFTER actual
authenticated rereads. See examples/revalidate.input.json for runnable data.

```text
fresh = {
  schema_version: 3,
  config: latest explicit RuntimeConfig,
  state_config_fingerprint: binding from the latest loaded state,
  state_status: "verified",
  state_generation: latest unchanged generation,
  connector_capabilities: actual unchanged Capabilities,
  source: latest direct source-ID observation,
  destination: latest destination-ID observation, or newly verified create absence,
  marker_inventory: rebuilt fresh inventory in the action.expected shape,
  reread_certificate: {
    complete: true,
    immediately_before_write: true,
    source_by_id: true,
    destination_by_id_or_create_absence: true,
    all_registered_destination_ids: true,
    current_window_pages_complete: true,
    marker_lookup_repeated_when_required: true
  }
}
```

Call `revalidate_action(action, fresh)` or CLI `--revalidate` with
`{"action": action, "fresh": fresh}`. All reread flags are required and truthful;
the planner has no clock/network and cannot itself prove that the caller performed
the reads. The last flag is true only after repeating the required create lookup,
or when no per-marker lookup is required for an already registered update/delete.
Never populate fresh by copying stale expected values.

The guard checks action integrity, verified state/generation, capabilities,
canonical fingerprints, observation completeness, marker uniqueness and exact
owned destination/protected values. Create requires freshly certified absence
and a complete per-marker lookup. Any mismatch/partial read/manual change returns
allowed false; replan. An allowed result supplies a real destination etag only in
conditional-write mode, otherwise null, plus explicit residual race information.
It is not a lock, a guarantee of future state, or permission to retry an unknown
write outcome.

## Write executor requirements and projection limitations

Create exactly title blocked, private, opaque, default event type, canonical sole
marker, no attendees/location/conference/reminders/recurrence. The current plugin
create arguments can express this with event_type default, add_google_meet false,
attendees [], self_attendance omit, reminders `{use_default:false,overrides:[]}`,
and the explicit title/description/visibility/transparency fields. Use timed or
all-day boundaries as appropriate; never mix them. Do not attach hidden metadata.

For UPDATE apply only start/end (and necessary boundary timezone arguments),
preserving every unspecified field. Use both boundaries when switching timed and
all-day forms. Do not pass a full replacement body or clear hidden provider
attributes. The desired object is an intent projection, not an API request: do
not blindly send conferenceData/other/unknown sentinels/all_day_bounds. Default
event type is enforced at creation; if unobservable later it cannot be independently
verified. Empty hangout_link is the exposed conference check; absence of hidden
conferenceData/extended properties/guest settings cannot be proven by this plugin.
Those residual limitations are explicit in each projection-mode result.

Manual changes to every observable non-time field, status or marker conflict,
including before deletion. Time-only mirror edits can be repaired. ASCII-only
whitespace changes to a sole marker are allowed at planning; if anything changes
between plan and reread, the strict fingerprint guard still requires replanning.

Deletes retain the original safeguards: persisted owned mapping, direct known-ID
source verification, unique observed marker and unchanged protected destination.
Cancellation, explicit deletion, decline, free status, or verified movement beyond
the future window can release a future mirror. A verified move from previously
future/ongoing to before run start can also release a still-future mirror. Already
ended-past mirrors are retained even after source cancellation/deletion; naturally
ended sources do not cause bulk cleanup. Missing search results never imply delete.

Journal intent durably before write. After write read back, verify the desired
observable fields/marker/ID and source identity, then persist verified snapshots
with the unchanged legacy raw identity and a new generation. On timeout, partial
write response, interruption or persistence failure mark state uncertain; recover
using journaled ID, marker lookup and known-ID reads. Multiple matches conflict.
An empty read after an ambiguous create does not by itself prove failure. Never
blindly retry. After verified deletion reconcile the journal/registry rather than
recreating from stale listing data.

Production adapter validation, real-data dry-run, persistence and verified cutover
remain with the parent. This package changes no real calendars, schedules,
authentication or unrelated repositories.

Self-attendee checks also validate the configured identity: see RUNTIME_CONFIG.md.
