# Evidence-bound recovery and command gating

Planner 1.3.1 / adapter 1.2.1; schema 3 / raw schema 2 / ledger version 2.
Existing inputs without recovery records retain their previous behavior and marker
hashes. This extension performs no persistence or external calls.

## Executor gates

The executor MUST check every command's exit status, JSON shape and required
success flag before executing the next command. A printed error followed by a
successful later command is a failure, not permission to proceed. Never use a
multi-command shell block's final status as proof that all earlier commands passed.

Before preparing an intent, perform the certified current rereads and call:

```sh
python3 planner.py --preflight preflight-request.json -o preflight-result.json
```

The request is `{action, fresh}`. `fresh` has the usual complete reread contract,
but its managed context is the unchanged ledger/admission from the plan and it
has **no creation_claim**. Exit 0 and `ready_to_prepare:true` together permit
preparing the journal. `allowed:false` and `calendar_call_allowed:false` always
remain false: preflight never authorizes a Calendar call. Failures exit 2.

Recheck reread fingerprints before persisting `attempt_started` whenever possible.
Persist/read back each exact transition only after all preceding commands and
guards passed. After durable attempt-started readback, the existing mandatory
`--revalidate` must exit 0 AND return `allowed:true`, in the same uninterrupted
root-admitted execution, before its single Calendar call. Any nonzero command,
malformed output, missing flag, uncertain persistence or failed guard stops the
sequence. The pure functions cannot enforce shell control flow or consume a call.

Do not add fields while rebuilding snapshots. A create absence snapshot is exactly:

```json
{"calendar_id":"calendar-beta@example.invalid","event_id":null,"outcome":"marker_absent","event":null}
```

It has no `evidence` key. A found source detail DOES have `evidence:null`. Adding
`evidence:null` to absence changes its fingerprint and fails both guards.

## Closed execution with positively recorded zero calls

An empty marker search, journal status, failed guard, absence of a returned event
ID or an operator's recollection does not prove zero calls. The root must examine
a complete durable execution trace recording dispatch and external calls, verify
that the old execution is closed, and positively establish **zero issued Calendar
calls, no dispatch begun, and no external-write uncertainty**. If any call was
issued, its outcome is unknown, or the operation is `uncertain`/`committed`, this
recovery path is unavailable. Reconcile positively by reads using the existing
process. Do not reclassify uncertain operations to make them pass this extension.

Keep the old operation and issued row. Append a durable recovery record to the
optional `ledger.recoveries` array, assign a new ledger generation and verify the
entire readback under a NEW root-serialized admission. Each record has exactly:

```text
{
  recovery_id: new unique ID,
  operation_id: old operation ID,
  marker: exact old marker,
  outcome: "aborted_before_call" | "verified_no_write",
  operation_fingerprint: digest(exact preserved old operation),
  prior_action: complete original create action, including its ID and snapshots,
  config_fingerprint: current canonical configuration fingerprint,
  observations_fingerprint: recovery_observation_fingerprint(current normalized input),
  authorized_admission_token: new root admission token,
  evidence: {
    kind: "executor_trace",
    trace_reference: reference to the actual durable trace,
    trace_sha256: SHA256 of its exact bytes, 64 lowercase hex,
    prior_admission_token: original action's serialized admission token,
    durable: true, trace_complete: true, execution_closed: true,
    calendar_calls_issued: 0,
    call_dispatch_started: false,
    external_write_uncertainty: false
  },
  consumed_by_operation_id: null
}
```

`digest` uses canonical sorted compact JSON; use the exported helper. The
observation helper binds canonical config, run start, capabilities, both complete
calendar observations, direct details and ownership state; it excludes the ledger
to avoid circular approval construction. Acquire fresh observations first, compute
this fingerprint, then append the certificate. This is a caller/root approval,
not a certificate that this offline package can issue for real execution.

The old operation may remain `prepared` or `attempt_started`; the appended outcome
resolves its zero-call ambiguity without overwriting history. Explicit historical
`aborted_before_call` is also accepted only with this full record. A bare
`verified_no_write` status still cannot authorize creation. Every record resolves
one operation once. Every recovery authorizes at most one NEW operation; its ID
and action ID must differ from the old ones. Do not resume/replay the old attempt.

The current source must be read by known ID, remain busy/in-window, and have the
exact original source snapshot. Destination absence and capabilities must equal
the original snapshots. Config and state binding must match. Both current windows
and all registered/ledger IDs must be complete; destination namespace and key
queries must be freshly exhausted with no time limits. Any changed current batch
or state invalidates its observations fingerprint. Window time may advance between
the failed action and explicitly certified recovery; the fresh batch is separately
bound. A changed source/destination is outside this narrow recovery path.

The issued row must be unresolved with no known destination IDs or mapping. Any
exact/suspect destination, untracked marker or manual change stops recovery. All
operations for this marker must have explicit zero-call resolutions, and the sole
unused recovery must resolve the latest operation. Other unresolved issued markers
continue to block managed creation conservatively; this is not a bulk recovery.

## One linked new attempt

The plan includes `expected.recovery` and its fingerprint and uses reason
`verified_zero_call_recovery_new_attempt`. Preflight uses the unconsumed record.
When constructing the new prepared ledger, preserve every issued row and all old
operations. Do not append the issued marker a second time. Instead:

1. Append `{operation_id:NEW, marker, status:"prepared", action_id:NEW_ACTION_ID,
   recovery_id:RECOVERY_ID}`.
2. Set only that recovery's `consumed_by_operation_id` to NEW.
3. Assign a new generation, durably persist and verify readback.
4. Follow the existing prepared-to-attempt-started transition and final guard.

The final guard reconstructs this exact change and rejects missing/changed old
history, reused IDs, missing links or unexpected edits. A consumed record cannot
be reopened, copied or used for another attempt. If the NEW attempt independently
fails with positively recorded zero calls, it needs a NEW trace, recovery record,
admission, action and operation. Preserve the entire chain. Positive success still
needs known-ID readback, verified mapping and committed ledger outcome.

This is conditional on trustworthy complete history and actual root serialization.
The planner cannot read trace bytes, prove truthful evidence or storage durability,
detect history omitted by a caller falsely claiming completeness, or prevent replay
of identical JSON by an executor. It never treats an empty search as proof of no
write. Indexed-search limitations and the final read/write race remain as documented
in MANAGED_COVERAGE.md. No authentication or additional backend is introduced.
