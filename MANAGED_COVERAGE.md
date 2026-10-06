# Managed creation coverage: planner 1.3.1 / adapter 1.2.1

This adds an explicitly narrower, truthful alternative to a universal marker
lookup. Planner schema 3 and raw adapter schema 2 require RuntimeConfig and support an OPTIONAL
`managed_context` top-level field and a discriminated marker-search scope. Universal coverage and registered reconciliation policies are unchanged; older input schemas require explicit metadata migration. Pin the new
source bytes: earlier releases do not implement this extension.

The production Google Calendar `q` search cannot certify universal marker
absence. Use `managed_state_window_and_indexed_search`. Its evidence is complete
managed history, known-ID reads, complete current-window observations, and
exhausted indexed queries. It is **not** `exact_marker_all_destinations`.

## Required coverage and its limits

For a new busy source occurrence, creation is proposed only when:

1. Both actual-run-start through +90-day lists (including ongoing events) are
   complete, with expanded occurrences and all candidate details.
2. Ownership state is verified and complete. Every registered source and
   destination is read by ID, including out-of-window and marker-damaged IDs.
3. A complete durable issued-marker AND operation ledger has verified history
   provenance. Every current mapping is represented. Every known destination in
   the ledger is read by ID, even after retirement or outside the window.
4. The requested marker has never been issued or reserved, OR the narrow
   evidence-bound zero-call recovery in [RECOVERY_CONTRACT.md](RECOVERY_CONTRACT.md)
   authorizes one new linked attempt. Issued history is retained forever.
5. All mapped ledger destinations have matching verified registry mappings and
   canonical observable current fields; manual non-time edits or missing/damaged
   mirrors block managed creates. Retired IDs need explicit known-ID terminal
   verification. All unresolved operations conservatively block managed creates,
   even when they concern a different marker. Existing registered reconciliation
   still follows the original rules and verified-state requirement.
6. On the destination calendar, run BOTH namespace and key queries with no time
   bounds, exhaust every returned page, and read every candidate in detail. The
   planner checks whole ASCII-normalized descriptions, not substring ownership.
7. No exact/suspect matches, malformed observed marker or untracked owned marker
   remain, including cancelled candidates that contradict never-issued history.
   Tracked IDs are excluded as sources even if their markers were erased.
8. Root coordinates admission of only one uninterrupted execution after a
   paused/drained cutover, or an actual atomic-claim backend is used. A boolean,
   generation label, JSON owner field or read/write readback is not itself a lock.

This mode cannot detect an **unknown manually copied-and-obscured out-of-window
mirror** that is absent from the complete managed ledger and not returned by either
indexed query. Indexing limitations, whitespace-tokenization and hidden edits can
cause such misses. Current-window copies are inspected; tracked copies are read by
ID. Do not describe this mode as universal duplicate detection. If this residual
is unacceptable, creation must remain blocked until true universal coverage exists.

## Normalized search

On the destination Calendar, each normalized `marker_searches` entry is exactly:

```text
{
  marker: canonical exact marker,
  scope: "managed_state_window_and_indexed_search",
  complete: true,
  next_page_token: null,
  queries: [
    {kind: "namespace", q: "<NAMESPACE_HEX32>",
     time_min: null, time_max: null, complete: true, next_page_token: null},
    {kind: "key", q: the marker's 64 lowercase hex SHA256 characters,
     time_min: null, time_max: null, complete: true, next_page_token: null}
  ],
  events: [all full normalized query candidates, deduplicated by ID]
}
```

`complete` means these indexed queries were exhausted, not that the index finds
every possible marker spelling. Different IDs with the same marker are preserved
and conflict. Identical IDs returned by both queries merge once after detail
consistency checks. Omitted queries, bounded queries, wrong terms and unread pages
fail closed. Only the two allowed calendars are accepted.

## Raw adapter search

The raw adapter accepts a second `RawMarkerSearch` variant:

```text
{
  marker: canonical exact marker,
  scope: "managed_state_window_and_indexed_search",
  complete: true,
  queries: [
    {kind: "namespace", q: namespace, time_min: null, time_max: null,
     complete: true, pages: [RawPage, ...]},
    {kind: "key", q: marker hash, time_min: null, time_max: null,
     complete: true, pages: [RawPage, ...]}
  ]
}
```

Use the actual request parameters. Null time bounds certify those parameters were
omitted from the authenticated query, including no implicit window restriction.
`RawPage` is the existing `{request_page_token, response:{events,next_page_token}}`
contract. Each query needs at least one page even when empty and must end in null.
Do not add `coverage_verified` or pretend indexed search is universal. Every query
candidate requires a complete actual detail response, with actual calls split into
batches of at most ten IDs. The raw top-level `managed_context` below is preserved
unchanged in normalized input; the adapter never creates, persists or guesses it.

## Complete durable ledger and external writer

`managed_context` is exactly:

```text
{
  single_writer: {mode: "serialized_runner", token: nonempty root admission ID,
                  root_coordinated: true, cutover_paused_and_drained: true,
                  only_one_execution_admitted: true},
  ledger: {
    version: 2,
    config_fingerprint: fingerprint of the canonical RuntimeConfig,
    status: "verified",
    complete: true,
    durable: true,
    generation: nonempty durable journal revision,
    provenance: {
      kind: "verified_history_and_state",
      history_complete: true,
      state_verified: true,
      evidence: nonempty reference to the actual complete-history and bootstrap audit
    },
    issued: [
      {marker, destination_calendar_id,
       destination_ids: [every historically known destination ID for this marker],
       disposition: "mapped" | "retired" | "unresolved"}
    ],
    operations: [
      {operation_id: globally unique nonempty ID within this ledger,
       marker, status: "committed" | "verified_no_write" | "prepared" |
                       "attempt_started" | "uncertain",
       action_id: exact originating action ID, or null for audited historical operations}
    ]
  }
}
```

All structural keys are required and extras are rejected. Each marker occurs once
in `issued`. Every issued marker needs operation history; every operation must
reference an issued marker. Destination IDs cannot be reused across entries or
treated as native sources. A mapped entry has exactly one destination matching a
current registry mapping and at least one committed operation. Retired entries
have no mapping, retain every prior ID, and need direct terminal verification for
those IDs; an audited definite no-write entry may have no IDs. `not_found`, error
or a missing response is insufficient. An unresolved entry alone cannot authorize
a create. Pending/uncertain operations require a nonempty action_id. Ledger version
2 optionally accepts `recoveries` and linked new operations with `recovery_id` as
defined in RECOVERY_CONTRACT.md; `aborted_before_call` requires that full proof.

The ledger is append-preserving history: never remove an issued marker or its
known IDs to make it eligible again. Update dispositions/operation status only
after explicit reconciliation. This offline program validates the supplied
snapshot; it cannot prove storage durability or detect a caller who omitted history
while falsely certifying completeness. Keep the actual durable audit journal too.

### Migration of an existing verified registry

The external worker must pause/drain writers and admit only one root-coordinated
execution (or acquire a real fence on a capable backend), read
the COMPLETE existing journal/state, and complete the current full bootstrap/readback
validation before certifying provenance. A prior successful test alone is not a
current journal audit. Verify no pending/uncertain operation or omitted historical
issuance exists. Preserve every verified marker identity and destination ID;
seed one mapped issued record and audited committed operation for each. Include
all other historical issued/reserved markers from the complete journal, including
retired ones; a current mapping count is not permission to truncate history.
Persist a new ledger generation durably and verify readback before enabling this
mode. Missing or uncertain history must remain blocked. **Never infer a blank
ledger from an empty listing, missing file, new planner state or empty query.**

The source has no migration writer and does not touch the live registry. Synthetic
fixtures use explicit synthetic audit certificates, never live-data claims.

## Execution capabilities: serialized production, optional atomic claim

Production persistence has no CAS or atomic-claim primitive. Use the
`serialized_runner` single_writer object shown above. `token` identifies the
root's current admission; it is a correlation value, not a lock or fencing token.
The root must pause/drain competing work for cutover and admit only one execution
at a time. It must not launch overlapping scheduled/manual/recovery writers.
The flags certify that actual orchestration, not atomicity of a persisted owner
field. No new authentication, service, lock server or local daemon is required.

Backends that really support atomic claims may instead supply:

```text
single_writer = {mode: "atomic_claim", externally_enforced: true, token: actual external token}
```

The prior two-key `{externally_enforced:true,token}` form remains a backwards-
compatible alias for atomic_claim. Do not use that alias for current production.
Unknown modes and switching modes/tokens between plan and guard are rejected.

## Durable intent and mandatory creation guard

Each managed create action adds `expected.managed_context` and its fingerprint.
`expected.marker_inventory.tracked_destination_observations` contains complete
sorted direct-ID observations for ALL ledger destination IDs.
`expected.marker_inventory.observed_marker_events` contains sorted found snapshots
for every owned/malformed description observed on either calendar, including
cancelled events. Rebuild these lists from fresh reads; new orphan/malformed
markers invalidate an action even if their hash differs. Existing snapshot and
state-generation checks remain. A supplied ledger is honored even with genuinely
universal coverage; scope changes cannot bypass issued history.

For **serialized_runner**, the root-admitted executor performs this sequence:

First follow the command-exit and preflight gates in RECOVERY_CONTRACT.md. Check
fresh fingerprints before durable intent/attempt-started whenever possible. Every
command and guard must succeed; never continue after a failed preparation command.

1. Verify the action and latest state/ledger revisions. For a never-issued marker, append exactly one issued
   row `{marker,destination_calendar_id,destination_ids:[],disposition:"unresolved"}`
   and one operation `{operation_id,marker,status:"prepared",action_id:action.id}`
   to the expected ledger. Use a new unique operation ID and prepared generation.
   Persist the intent with ordinary durable storage, read it back and verify the
   full intended snapshot. Preserve all prior issued markers/history.
   For an approved zero-call recovery, preserve the existing issued row and consume
   the recovery with one new linked operation as defined in RECOVERY_CONTRACT.md.
2. Immediately reread the source, both complete current windows, every registered
   and ledger destination ID, both unbounded indexed queries and candidate details.
   Rebuild the expected-shaped inventory, check unchanged ownership state and that
   this remains the same single root-admitted execution. Abort on any mismatch.
3. In that same uninterrupted execution, change only this new operation from
   prepared to attempt_started and assign another generation. Persist durably and
   read back the exact resulting ledger BEFORE any Calendar create call. This is
   an ordinary serialized write/readback, **not CAS or an atomic claim**. If either
   persistence or readback is uncertain, do not call Calendar.
4. Call `revalidate_action(action,fresh)` with the usual fresh contract plus:

```text
fresh.managed_context = {
  single_writer: same actual root admission object,
  ledger: latest durable ledger AFTER attempt_started readback
}
fresh.creation_claim = {
  mode: "serialized_runner",
  operation_id: new operation ID,
  action_id: action.id,
  prepared_generation: revision from step 1,
  prepared_ledger_fingerprint: snapshot_fingerprint(entire step-1 prepared ledger),
  durable_intent_persisted: true,
  intent_readback_verified: true,
  attempt_started_readback_verified: true,
  same_uninterrupted_admitted_execution: true,
  calendar_call_not_yet_attempted: true,
  resume_or_retry: false,
  from_status: "prepared",
  to_status: "attempt_started"
}
fresh.reread_certificate additionally requires:
  all_ledger_destination_ids: true,
  ledger_and_single_writer_reread: true
```

`creation_claim` is a retained field name; in serialized mode it records evidence
of the ordinary durable transition and admission, not an atomic claim. Its exact
keys contain NO `atomic_once_only_claim` flag. The action's write_contract declares
`creation_attempt_mode:"serialized_runner"`,
`durable_intent_and_attempt_started_readback_required:true` and
`root_serialized_one_call_no_resume_required:true`; it does not require atomic CAS.

5. Only exit 0 AND an allowed guard result in this same uninterrupted first-attempt path
   permits ONE authenticated Calendar call. Consume that code path before calling;
   never loop or replay it. Read back and verify the returned ID/marker/fields.
   Then durably record the verified mapping and committed ledger outcome under
   the existing journal recovery procedure, verifying each readback. No atomic
   transaction across those persistence writes is claimed or required: a partial
   commit keeps the item uncertain and held for reconciliation. Keep the issued
   marker forever and replan remaining actions against new revisions.

### Conservative at-most-once recovery

A crash, interruption, timeout, partial response, uncertain persistence or an
unknown Calendar outcome means HOLD FOR RECONCILIATION. A later execution must not
resume a prepared/attempt_started operation or mark `calendar_call_not_yet_attempted`
true based solely on a journal state or empty search. Recover with known-ID reads,
marker searches and the complete journal. Even a crash after attempt_started but
before the call does not by itself justify another call. Previously reserved/issued
markers remain blocked unless a complete recorded zero-call execution trace and
fresh root approval satisfy RECOVERY_CONTRACT.md. A bare verified_no_write status
does not re-enable creation. No automatic retry, old-attempt replay or history
clearing path exists. Actual issued/uncertain external calls remain blocked here.

This is conservative at-most-once behavior **conditional on root serialization**,
not atomic exactly-once execution. The pure stateless guard cannot consume an
attempt or distinguish identical JSON replay: repeated inputs can return allowed
again. The root/executor must never treat that as permission for a second call.
If root coordination can no longer ensure one admitted writer, stop mutations;
do not pretend a persisted owner flag or ordinary write/readback solved concurrency.

### Optional atomic-claim mode

Only a backend that truly supports atomic claims may use atomic_claim. After the
durable prepared intent, atomically consume prepared -> attempt_started once,
with a new revision under its actual external writer mechanism. Use the same
common creation_claim fields (operation_id, action_id, prepared_generation,
prepared_ledger_fingerprint, durable_intent_persisted, from_status, to_status) plus
`atomic_once_only_claim:true`; omit the serialized mode/flags. The guard retains
the exact action-bound two-stage ledger validation. This optional mode is not a
requirement for the current production backend.

Both modes retain the complete history, unbounded query and known-ID protections.
Only one create can proceed from a particular ledger snapshot; subsequent actions
must replan after history changes. Neither mode removes the connector's final
reread/write race with manual calendar edits or the documented unknown obscured
out-of-window mirror limitation.

Runnable managed-create and managed-revalidate examples now demonstrate
serialized_runner with no CAS and no atomic claim. Synthetic true flags are not
production evidence. No calendars, live journal/state, schedules or auth were
changed by this package.

RuntimeConfig and mandatory ledger version-2 config binding are defined in
[RUNTIME_CONFIG.md](RUNTIME_CONFIG.md). Never silently bind an older ledger.
