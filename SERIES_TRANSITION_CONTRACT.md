# Reviewed recurring-series transition: state-only contract v1

Planner 1.4.0 and adapter 1.3.0 accept an optional `series_transitions` array in
their existing normalized schema-3/raw schema-2 envelopes. With no certificate,
`source_series_identity_changed` remains a conflict. Supplying a certificate
selects a dedicated review mode: **no Calendar create, update or delete is
returned**, including for unrelated items in that batch. This feature produces
an audited proposal to replace only each registered mapping's `verified_source`.
It does not persist state, fetch evidence, authenticate or execute Calendar calls.

Google documents a "this and following" edit as trimming the old recurrence and
creating a new recurring event. Its instance `originalStartTime` identifies the
nominal occurrence even after a move. That does not prove stable instance IDs for
any particular split; this contract requires actual observed stable IDs and raw
original strings. See the [Google recurring-events guide](https://developers.google.com/workspace/calendar/api/guides/recurringevents)
and [event resource](https://developers.google.com/workspace/calendar/api/v3/reference/events).

## Evidence and supported scope

The external reviewer obtains actual master details and fully paginated instance
reads using the existing direct Google Calendar plugin. Every instance read must
include deleted events. Project the returned evidence into the records below;
do not invent missing fields or insert recurring masters into ordinary expanded
event listings. Preserve complete read-only evidence externally and bind it by
reference and SHA-256. Synthetic fixture builders are **not evidence collectors**.

This first implementation intentionally accepts only all of these conditions:

- Two distinct self-owned, confirmed, timed masters in one allowed source calendar;
  the verified RuntimeConfig self identities and explicit connector `is_self:true`
  agree. Every instance carries the same ownership evidence.
- One `RRULE:FREQ=WEEKLY` per master; interval absent or `1`; one matching
  weekday or no BYDAY; optional valid WKST; optional UTC UNTIL or positive COUNT
  (never both). The new continuation may have neither and continue indefinitely.
  No RDATE/EXDATE/additional rules. An actual IANA timezone must be available.
- Old master has UNTIL before the split and its last nominal occurrence is exactly
  one local week before the new master's first occurrence. Same timezone, weekly
  pattern, local start time and elapsed duration. Old starts before the split;
  new starts at the split instant.
- **Timestamp evidence:** `old.created == new.created` and
  `old.updated <= new.updated` as instants, bound to fresh master reads and the
  reviewed evidence. Both masters may preserve the same historical created and
  updated timestamps; `new.created == new.updated` is never required. An
  independently created master with a different created timestamp is rejected.
- Exhausted old/new instance reads use either the exact current run-start/+90-day
  window or, for a finite master only, no time bounds. No cancelled entries,
  duplicate IDs, duplicate nominal instants, aliases or overlap. Old nominal rows
  all precede the split. Old UNTIL includes the nominal week immediately before the
  split and excludes the split itself. New rows exactly equal all registered
  occurrences of the old series at/after the boundary. Partial subsets are rejected.
- Every affected occurrence keeps its exact event ID, byte-identical raw
  `original_start_time`, current start/end, marker, destination ID, response and
  confirmed status. A previously moved exception is supported when both its nominal
  identity and already-moved current times are unchanged. Source must currently be
  busy in the normal run-start/+90-day/ongoing window. Mirror times and every
  observable protected field must still equal the verified baseline.
- Exactly one canonical registered mirror per marker; no suspicious marker
  description or competing ID. Ledger has its mapped issued row and committed
  operation history. State, ledger, known-ID details, page coverage and root
  serialization satisfy the existing full contracts.

All-day splits, monthly/daily/multi-weekday rules, interval greater than one,
changed time/response, out-of-window affected occurrences and
partially mapped new series remain unsupported conflicts. They never fall back to
delete/recreate. Normal all-day and recurrence synchronization outside this review
mode remains unchanged.

A bounded collection proves only its requested current window, including ongoing
events (`end > time_min` and `start < time_max`). It need not start at the master's
first historical occurrence or prove that an infinite series ends. Every returned
row must intersect those bounds, and every known in-scope instance must appear in
the pages. Known-ID source/destination snapshots and the exact registered target
set provide a second check. Nominal gaps alone are not rejected for bounded reads:
moved exceptions can leave a nominal slot while changing no identity. The caller
must truthfully certify full pagination; the offline code cannot discover a page
that the caller falsely claims does not exist.

The old bounded collection may be empty when all pre-split instances are past;
its RRULE still proves the adjacent pre-boundary truncation. An optional unbounded
read of a finite master must include every nominal slot through COUNT/UNTIL.
An unbounded read of an infinite master is rejected. Old and new collections may
use different supported scopes; neither scope authorizes global absence inference.

Old/new iCalUID values may differ. Each instance must match its respective master;
any exposed UID in the saved/current source snapshot must match the old/new master
respectively. A UID change alone neither authorizes nor rejects a transition.

## Exact certificate shape

All keys shown are required; unknown keys are rejected. `series_transitions` must
be nonempty when supplied. Transition IDs must be unique; certificates cannot
overlap. `digest(value)` means SHA-256 of UTF-8 JSON with sorted object keys,
compact separators, `ensure_ascii=False`, and no nonfinite numbers, as implemented
by `planner.digest`. This differs from the unchanged ordered-array marker recipe.

```text
{
  version: 1,
  kind: "following_events_split",
  transition_id: nonempty audit identifier,
  config_fingerprint: planner.config_fingerprint(config),
  state_generation: exact current verified state generation,
  source_calendar_id: exact allowed calendar ID,
  split_original_start_time: explicit-offset RFC3339,
  old_master: Master,
  new_master: Master,
  old_instances: InstanceCollection,
  new_instances: InstanceCollection,
  occurrences: [OccurrenceProof, ...],
  review: {
    status: "approved",
    connector: "google_calendar_direct",
    evidence_reference: nonempty durable read-only evidence reference,
    evidence_sha256: 64 lowercase hex of that external evidence,
    readonly_evidence_verified: true,
    previous_writers_drained: true,
    admission_token: exact managed_context.single_writer.token,
    timestamp_relation: "shared_created_and_ordered_updates"
  }
}

Master = {
  calendar_id: source_calendar_id, event_id: returned master ID,
  status: "confirmed", organizer: {email: verified self email, is_self: true},
  created: RFC3339, updated: RFC3339,
  start: explicit-offset RFC3339, end: explicit-offset RFC3339,
  time_zone: IANA name, recurrence: [single weekly RRULE string],
  i_cal_uid: actual nonempty returned UID
}

InstanceCollection = {
  calendar_id: source_calendar_id, master_id: corresponding master ID,
  show_deleted: true,
  time_min: exact run start (or null only for an unbounded finite-master read),
  time_max: exact run start plus 90 elapsed days (or null with time_min null),
  complete: true,
  pages: [{
    request_page_token: null first, otherwise previous next_page_token,
    response: {
      events: [Instance, ...],
      next_page_token: nonempty continuation token or null only when exhausted
    }
  }, ...]
}

Instance = {
  calendar_id: source_calendar_id, event_id: actual instance ID,
  recurring_event_id: corresponding master ID,
  original_start_time: exact raw explicit-offset RFC3339,
  status: "confirmed", start: raw RFC3339, end: raw RFC3339,
  organizer: {email: verified self email, is_self: true},
  i_cal_uid: corresponding master's returned UID
}

OccurrenceProof = {
  source_event_id: unchanged registered instance ID,
  original_start_time: unchanged exact registry raw string,
  marker: unchanged registered marker,
  destination: {calendar_id: registered other calendar, event_id: unchanged ID},
  mapping_fingerprint: digest(complete current saved Mapping),
  source_fingerprint: digest(complete normalized found source Snapshot),
  destination_fingerprint: digest(complete normalized found destination Snapshot)
}
```

Snapshots are the existing `Planner.source_snapshot((calendar_id,event_id))`
records, including outcome/event/evidence, not merely the inner event. Use the
normalized adapter output to calculate these fingerprints. The adapter preserves
the supplied certificate verbatim and validates it; it never supplies omitted
approvals, page completion, timestamp, ownership or evidence assertions.

Any known-ID observation contradicting an instance page blocks the certificate.
Equivalent formatting of raw original strings is insufficient in this special
mode. Normal registry formatting compatibility elsewhere is unchanged.

## Result and final guard

A valid and otherwise conflict-free batch returns `status:"series_rebind_ready"`,
`calendar_call_allowed:false`, ordinary `noop` actions for affected occurrences
with reason `reviewed_series_split_requires_state_rebind`, and `series_rebinds`.
Each proposal contains `op:"rebind_series_state"`, an integrity `id`, transition
ID, reason, `calendar_call_allowed:false`, replacement mappings, and audit fields.
Its `expected` binds config, complete state/generation, complete managed context,
connector capabilities, complete certificate, and each mapping/source/destination
and managed marker inventory fingerprint. Only `verified_source` differs in each
replacement; source identity, marker, destination and verified destination remain
exactly as stored. Issued rows, operations and recovery history are untouched.

An unrelated planned Calendar mutation becomes a conflict and withholds **all**
series proposals. Any ordinary conflict also withholds them. Invalid/incomplete
certificates return a blocked result (or AdapterError), never a partial adoption.

```sh
python3 production_adapter.py examples/series-split.raw.json -o /tmp/series-input.json
python3 planner.py /tmp/series-input.json -o /tmp/series-plan.json
python3 planner.py --revalidate-series examples/series-revalidate.input.json
```

The pure API is `planner.revalidate_series_rebind(proposal, fresh)`, where:

```text
fresh = {
  data: complete freshly re-read normalized input, including the reviewed certificate,
  reread_certificate: {
    complete: true, immediately_before_state_write: true,
    source_and_destination_ids: true, masters_and_instance_pages: true,
    state_and_ledger: true, single_writer: true
  }
}
```

CLI input is `{proposal, fresh}`. CLI exit 0 requires `state_write_allowed:true`;
failure exits 2. Successful and failed results both retain `allowed:false` and
`calendar_call_allowed:false`. The guard replans fresh input and requires exact
proposal equality; an altered source, mirror, certificate, state, ledger or writer
token prevents the old proposal from passing. Use the same logical run start while
re-reading this attempt; if it changes, obtain and review a new plan.

## External state adoption

1. Pause/drain previous writers through the cloud root's actual admission mechanism.
   Review real evidence and admit one serialized state-only attempt. Boolean JSON
   assertions or readback are not locks. Production execution is external to this
   repository and must not use fixture claims.
2. Gate adapter/plan command exit and JSON status. Persist a separate durable audit
   of certificate, proposal, before-state hash and admission identity. Do not extend
   the strict State or Ledger schemas with ad hoc audit fields.
3. Immediately reread both calendars, all required details/marker coverage, masters,
   every instance page, state and ledger. Run the state guard and check its exit and
   `state_write_allowed:true`. No Calendar dispatch branch exists in this mode.
4. Under the same admission, replace exactly the proposed mapping baselines and
   advance state generation. Preserve all other mappings and the entire ledger.
   When several certificates are returned, adopt them as one state revision after
   validating each against the same untouched revision; never mix partial results.
5. Read back/verify the complete new state and record its hash in the durable audit.
   An uncertain persistence outcome holds execution for read-based reconciliation;
   never overwrite/reapply blindly. Remove the consumed certificate from subsequent
   planner inputs, retain it in audit history, then replan before resuming normal work.

The synthetic second-run test yields ordinary noops with all original mirror IDs
and markers preserved. Replaying the old certificate against the new generation is
rejected. The pure guard cannot prove external evidence truth, implement durable
storage/CAS, serialize writers or detect identical-input replay. Those remain root
executor responsibilities. No actual calendar/configuration/ledger/schedule change
is performed by this package, and no new authentication is required.

The packaged example and regression use the reported production shape with wholly
synthetic values: shared historical created timestamps, shared later updated
timestamps, `RRULE:FREQ=WEEKLY;BYDAY=TU` without UNTIL/COUNT on the new master,
thirteen stable in-window instances, different old/new iCalUID, and unchanged
mirrors. Both a finite complete old read and an empty exhausted bounded old read
are tested. No live data is included.
