# Portable calendar projection adapter 1.3.2

This is a reusable, standard-library Python adapter for the existing direct Google
Calendar plugin. It converts certified raw connector observations into the exact
schema-3 input required by **planner 1.4.2**. It reads JSON files or
stdin and writes JSON files or stdout. It does not authenticate, call the network,
read calendars, mutate calendars, modify registry state, run a scheduler, read the
system clock, or create a daemon.

This release adds optional top-level `managed_context` and the truthful
`managed_state_window_and_indexed_search` search variant. See
[MANAGED_COVERAGE.md](MANAGED_COVERAGE.md) for the exact ledger, indexed-query,
migration and serialized durable creation-attempt contracts. The companion
[PLANNER_CONTRACT.md](PLANNER_CONTRACT.md) documents the full planner schema.

The authenticated caller must gather the observations and truthful certificates.
The adapter validates them offline. No runtime dependency on the development
computer or its credentials exists.

Planner 1.4.1 also handles a definitely cancelled/deleted source whose owned mirror
has already ended. Continue supplying registered source/destination detail reads
even when neither appears in the current window. The existing raw `found` cancelled
event or terminal result with verified-known-ID proof is sufficient; no new cleanup
field is accepted. An ambiguous missing result or incomplete response is never
converted into cancellation/deletion evidence. Raw/state/ledger schemas are unchanged.

Planner 1.4.2 isolates protected-content edits on identifiable registered mirrors
from unrelated managed creates. Preserve the actual edited reminders/content in
raw detail responses and current inventory; do not replace them with stored
canonical fields. The edited item remains held. This changes no raw fields or
adapter normalization and does not weaken complete detail/query requirements.

## Portable execution

Keep `production_adapter.py` and the pinned `planner.py` together:

```sh
python3 production_adapter.py raw-input.json -o planner-input.json
python3 planner.py planner-input.json -o plan.json
```

The adapter CLI exits 0 only after producing valid schema-3 input. On any incomplete
or invalid input it exits 2 and writes only:

```json
{"adapter_status":"blocked","reason":"stable_reason_code","planner_input":null}
```

Never pass a partial result to the writer. `adapt(raw)` is a pure Python API returning
the planner input or raising `AdapterError`. The raw input and supplied state are
deep-copied, not modified. A valid adapted input can still lead to a planner conflict;
for example, an explicitly missing source or uncertifiable creation lookup is an
item-level conflict, not necessarily a malformed collection.

Verify MANIFEST.json before execution. The adapter checks the exact included
planner source bytes using its PINNED_PLANNER_SHA256. This is package integrity,
not a deployment identifier. See RUNTIME_CONFIG.md for the required configuration
schema, verified self-identity checks, and old-state migration. Real runtime
configuration is supplied by the caller outside the package.

## Raw JSON envelope

All envelope/calendar/batch/page/certificate fields shown below are required unless
explicitly described otherwise. Unknown keys in those structural records are rejected.
Raw detailed event dictionaries may contain additional exposed fields; all additional
nonnull fields are retained except the explicit read-only profile fields below.

```text
{
  adapter_schema_version: 2,
  config: RuntimeConfig,
  run_started_at: actual explicit-offset RFC3339 run-start string,
  calendars: [RawCalendar(A), RawCalendar(B)],
  state: VerifiedPlannerStateWrapper | LegacyBootstrapWrapper,
  managed_context: optional explicitly audited ledger and externally enforced writer,
  series_transitions: optional reviewed state-only split certificates
}
```

The optional `series_transitions` value is copied verbatim into planner input and
fully validated by the pinned planner. Its supplemental master/instance projections
must come from actual externally verified direct-plugin reads. They are not ordinary
expanded source events. The adapter does not invent missing evidence or approvals.
See [SERIES_TRANSITION_CONTRACT.md](SERIES_TRANSITION_CONTRACT.md) for exact records,
supported weekly scope, page completion, ownership, audit and state-only guard.

A and B are the two exact IDs in the validated RuntimeConfig allowlist. No personal calendar, third calendar or `primary`
alias is accepted. The caller must choose the corresponding authenticated account
link; link IDs/credentials are not part of this offline contract.

```text
RawCalendar = {
  calendar_id: A or B,
  listing: {
    complete: true,
    expanded: true,
    includes_ongoing: true,
    time_min: actual run start,
    time_max: run start plus exactly 90 elapsed days,
    pages: [RawPage, ...]
  },
  detail_batches: [DetailBatch, ...],
  marker_searches: [RawMarkerSearch, ...],
  all_day_timezones: {
    event_id: {time_zone: IANA name, verified: true, evidence: nonempty reference},
    ...
  }
}

RawPage = {
  request_page_token: null for first page, otherwise previous next_page_token,
  response: {events: [raw search event, ...], next_page_token: string or null}
}
```

`response` is the tool's structured search response (`structuredContent.events` and
`next_page_token`). It may carry additional response metadata. If `has_more: true`
is present, a null next token is rejected. Keep the actual token used for EACH request;
the adapter does not infer missing intermediate calls. The first request token must
be null, subsequent tokens must match exactly, tokens cannot repeat, and a complete
search must end in null. At least one page is required even for an empty complete
search. Duplicate event IDs across pages fail closed rather than hiding a paging race.

The 90-day list must contain expanded occurrences and ongoing events, and retain
native busy/free/declined events. The adapter does not expand recurrences. An event
whose detailed `recurrence` is nonempty is rejected. No all-time native recurrence
expansion is required.

## Complete direct-ID reads and the connector's batch cap

```text
DetailBatch = {
  requested_ids: [1 to 10 distinct actual requested event IDs],
  complete: true,
  responses: [
    {event_id, outcome, event, evidence}, ...
  ]
}
```

The current `batch_read_event` silently caps results at ten. **The caller must split
actual authenticated calls into at most ten IDs each.** More than ten requested IDs
in one batch is rejected even if eleven response objects were supplied. Single-event
reads can be represented as one-ID batches. Empty detail_batches is valid only when
no required IDs exist.

Every requested ID must appear exactly once in that batch's responses, and the
returned event's actual ID must match. Ordering is not used to guess identity.
No unexpected response or duplicate request across batches is allowed. Nine returned
results for ten requests is an incomplete batch, never a missing/deleted event.
Never fill an omitted response with a guessed tombstone.

Read every current listing candidate, every marker-search candidate, and every
persisted registered source AND destination ID, plus ALL issued-ledger destination
IDs in managed mode, including out-of-window or marker-damaged IDs. The
adapter and pinned planner check this accounting. A registered source absent from
the listing must still have its explicit known-ID response.

| outcome | event | evidence |
| --- | --- | --- |
| found | raw full detailed projection | null |
| cancelled | null | verified known-ID cancellation proof |
| deleted | null | verified known-ID deletion proof |
| not_found | null | null; ambiguous absence |
| error | null | null; read/permission/transport failure |

Terminal proof is exactly
`{"kind":"deleted","verified_known_id":true,"proof":"nonempty-reference"}`,
or the analogous cancelled form. It must come from actual caller verification for
that exact ID. A 404, an absent search hit, or a silently truncated response is not
deletion proof. Sparse cancellation tombstones must be explicitly classified by the
caller, not passed as incomplete found Events. If identity cannot be established,
the batch cannot be certified complete.

Every listed candidate must have a successful detailed read. If it disappears or
changes between search and detail, recollect/replan; the adapter rejects that mixed
snapshot. Missing sources that were already absent from the listing, and verified
out-of-window source moves, are supported through direct-ID outcomes and passed to
the planner's existing conservative deletion/conflict logic.

## Raw search and detail projection fields

Search events must include these comparison fields:

```text
id, summary, description, status, start, end, original_start_time,
recurring_event_id, transparency, event_type, location, my_response_status
```

All are compared against detailed observations; self response is compared against
the full attendee-derived value. Null detailed summary and empty search summary both
mean an untitled event and normalize to `""`. Null/empty description also normalizes
to the verified empty description. Nonempty descriptions, including all marker text
and whitespace, are preserved verbatim. Other compared values remain exact.

The detailed connector profile requires every
one of these keys to be present, including explicit nulls:

```text
guests_can_modify, locked, id, summary, status, organizer, description, location,
color_id, event_label_id, start, end, attendees, url, hangout_link, event_type,
transparency, visibility, attachments, reminders, recurrence, recurring_event_id,
original_start_time, display_url, display_title
```

A missing field is a partial/unreviewed projection and fails closed. Search omissions
are never filled with guessed detail values. Raw start/end/original_start_time are
flat strings (or null for original_start_time), not Google dateTime objects.

Normalization rules:

- `summary: null` becomes `""`; `description: null` becomes `""` after the required
  detailed key was actually returned.
- Every attendee must include `is_self` and `response_status`. Explicit `is_self:
  null` becomes false. More than one true self-attendee is rejected. Self response
  is derived from that attendee, or `none` when none exists; it is checked against
  the search hint. The complete attendee dictionaries are retained and sorted
  canonically for stable snapshots.
- Reminders must return `use_default` as a boolean and `overrides` as an array/null.
  Rename to `useDefault`; null overrides becomes `[]`. Retain every additional
  nonnull reminder property. A simultaneous raw `useDefault` key is ambiguous and
  rejected. Override arrays are sorted canonically.
- `event_type` is preserved as observed `eventType`; explicit null is represented
  by `{"unobserved":true}`, not guessed default. `hangout_link` is retained, with
  a confirmed empty string/null normalized to null. Invalid non-string links fail.
- `conferenceData` is explicitly unknown. No provider etag is invented. Output
  capabilities are snapshot_reread / provider_etag false / conditional_writes false /
  google_calendar_projection, and Event.etag is null.
- `other.observed` contains every additional NONNULL exposed property, including
  guests_can_modify, locked, color_id, event_label_id and attachments. Preserve
  false, zero, empty array and empty object; only null is omitted. Newly exposed
  nonnull keys are retained as well, so they cannot silently bypass manual-change
  protection. The explicit read-only/computed exceptions are organizer, url,
  display_url and display_title, matching the declared connector profile.
- `other.unobserved_provider_fields` remains true. This never asserts that hidden
  provider properties are absent. Writes must still preserve unspecified fields.

The adapter never removes or adopts markers. Whole-description ownership, loop
exclusion, duplicate detection, manual edits and deletion safety remain in the
pinned planner. Raw occurrence ID and original-start string are preserved.
The registry's original raw identity is never replaced with a reformatted current
response; the planner handles equivalent reformatting against the stored raw key.

## All-day dates and zoneinfo

Timed start/end retain their exact RFC3339 offset strings as `{dateTime: raw}`. No
timezone name is guessed from an offset. Date-only start/end become `{date: raw,
timeZone: verified_name}`. Mixed timed/date boundaries are rejected.

For EVERY date-only event, supply an event-ID-specific all_day_timezones certificate
on its calendar. The caller must verify the effective event/calendar timezone using
authenticated evidence. An offset guess, computer timezone, global default, or an
uncertified timezone name is insufficient. The same certificate resolves legacy
stored all-day dates for the source when explicitly bootstrapping.

The adapter uses standard-library `zoneinfo.ZoneInfo` and resolves each local
midnight separately. DST days may be 23 or 25 hours. Date boundaries and exclusive
end date are preserved. It rejects unavailable timezone data/names and ambiguous or
nonexistent local midnight instead of choosing a fold or shifting a boundary.

The cloud runtime needs an IANA zoneinfo database (commonly supplied by the OS).
If necessary the operator can provide a verified TZif database directory through
`PYTHONTZPATH`; the adapter does not download/install data. Tests require zoneinfo
data for Asia/Tokyo, America/New_York, America/Havana and Pacific/Apia and do not
silently skip the DST cases. Windows development tests used a task-local TZif
database; it is not a production credential or daemon and is not bundled into the
release. Strict timestamp support is inherited from the planner.

## State modes: never edit the registry

Normal production input supplies the complete, already verified planner state:

```text
state = {
  mode: "planner",
  value: {
    status: "verified", generation: actual revision,
    config_fingerprint: fingerprint of canonical RuntimeConfig,
    mappings_complete: true,
    mappings: [complete planner-schema-3 verified mapping, ...]
  }
}
```

The adapter preserves the supplied mapping keys, raw original identities,
destination IDs and historical verified snapshots. It does not regenerate baselines
from current data. Missing, incomplete or uncertain state fails closed; no implicit
bootstrap or reset occurs. The planner checks every stored mapping/hash/baseline and
requires direct source/destination detail responses. Generation is not an atomic lock.

For an explicitly authorized legacy bootstrap only:

```text
state = {
  mode: "legacy_bootstrap",
  complete: true,
  registry_json: "the original complete UTF-8 legacy registry text"
}
```

Keep the original text unchanged, including whitespace, so its SHA-256 identifies the
actual captured bytes. This is the supported legacy registry shape, not the current
planner state. Required top-level legacy fields: namespace, phase, owner, errors,
updated_at, config.calendars, records. Namespace must match; phase must be active,
owner explicitly null and errors empty. These are consistency prerequisites, not an
atomic lock or proof there are no external writers. The parent still coordinates
pause/drain and any migration persistence separately.

config.calendars may use any TWO caller aliases, whose calendar_id values must exactly
match the allowlist. There is no hardcoded MT/TV routing. Every legacy row requires:

```text
source, destination, source_event_id, destination_event_id,
source_original_start_time, source_recurring_event_id, start, end, all_day,
source_status, source_response_status, state, error, marker, key, last_verified_at
```

Rows must be verified with explicit error null. Hash and marker are recomputed from
the unchanged occurrence identity; stored key must equal the marker hash. The
all_day flag must agree with stored boundaries. If a source is returned, its series
ID must match the stored series identity. Known missing/terminal outcomes are passed
to the planner, which blocks unsafe bootstrap. Caller-verified timezone evidence is
required for legacy date-only boundaries.

The adapter emits planner status bootstrap with legacy evidence bound to the registry
SHA-256 and last_verified_at. It does not invent historical full snapshots. The
planner must independently return bootstrap_ready and bootstrap_complete before the
parent may consider persisting its new readback baselines. This adapter never persists
that output and never edits the migrated state managed by another worker.

## Optional per-marker lookup evidence

Existing registered mappings and legacy bootstrap can use marker_searches `[]`.
For genuinely UNIVERSAL coverage, supply:

```text
RawMarkerSearch = {
  marker: exact canonical v1 marker,
  scope: "exact_marker_all_destinations",
  complete: boolean,
  coverage_verified: boolean,
  evidence: nonempty truthful coverage reference when complete,
  pages: [RawPage, ...]
}
```

Complete true requires coverage_verified true, nonempty evidence and a complete page
chain ending in null. Page completion alone does not prove coverage. A broad query
matching the hash can miss ASCII-spaced markers; never fabricate a coverage assertion.
The current indexed `q` connector cannot establish this universal certificate.
Use the explicit managed search variant in MANAGED_COVERAGE.md together with all
ledger and external-writer prerequisites, or omit the search/use complete false
and block the create. Never infer coverage from page exhaustion alone.

Read all lookup candidates in detail, including extra-text/malformed descriptions.
The adapter preserves candidates; the planner determines ownership by an anchored
whole normalized description, never a substring. Lookup results can include
out-of-window candidates without an all-time native recurrence expansion.

## Execution safeguards

The generated planner input is a proposal source, not authorization to blindly write.
Use the pinned planner's pre-write revalidation contract: immediate fresh source,
destination, window and registered-ID reads, required repeated marker lookup,
canonical snapshot comparisons, verified state generation and ownership checks.
Any change requires replanning. Without provider CAS a final reread/write race remains.
Journal writes, verify readback, and treat timeout/unknown outcome as uncertain;
never blindly retry. Managed creation supports serialized_runner: root admits one execution after
paused/drained cutover, persists and reads back intent then attempt_started before
one call, and holds all crashes/unknown outcomes for read-based reconciliation.
It requires no atomic CAS; atomic_claim remains optional for capable backends.
See MANAGED_COVERAGE.md for exact guard fields. The adapter implements no writer,
migration, lock or authentication.

RECOVERY_CONTRACT.md defines the optional durable zero-call recovery record and
new linked attempt. The adapter preserves this supplied record without inventing
evidence. It also preserves compact ID/hash-linked supersessions of unused records
without expanding, refreshing or rewriting their inherited evidence. Gate every
command's exit/result; use planner --preflight before intent
and compare fingerprints before attempt_started whenever possible. Final
--revalidate exit 0 AND allowed:true remain mandatory before the single call.

The release includes runnable synthetic raw/normalized/plan examples, managed
creation/pre-write examples, complete regression tests, a test report and file
checksum manifest. TEST_RESULTS.txt records the exact verified test counts.
No live calendar data is present.
