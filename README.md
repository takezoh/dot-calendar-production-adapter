# Portable calendar synchronization planner and adapter

Adapter **1.3.1** pins planner **1.4.1**. Python 3.10+ standard library, with IANA
timezone data available to `zoneinfo` for all-day events and series review. Linux normally supplies
this data; other runtimes may provide it through `PYTHONTZPATH`. Missing timezone
data fails closed. No credentials, authentication implementation, network client,
local daemon or calendar executor are included.

This repository contains only generic source, synthetic `example.invalid`
fixtures, contracts, tests and configuration templates. Clone and pin a verified
commit before running in the cloud. Keep actual configuration, calendar reads,
state, operation ledger, traces and generated plans **outside the checkout**.
Use the existing authenticated Google Calendar plugin for every external read/write.

## Verify and run

From the checkout root:

```sh
python3 verify_release.py
python3 production_adapter.py examples/timed-create.raw.json -o /tmp/calendar-normalized.json
python3 planner.py /tmp/calendar-normalized.json -o /tmp/calendar-plan.json
python3 planner.py --preflight examples/recovery-preflight.input.json
python3 planner.py --revalidate examples/recovery-revalidate.input.json
python3 planner.py --preflight examples/recovery-refresh-preflight.input.json
python3 planner.py --revalidate examples/recovery-refresh-revalidate.input.json
python3 planner.py --revalidate-series examples/series-revalidate.input.json
python3 planner.py --preflight examples/past-cancelled-preflight.input.json
python3 planner.py --revalidate examples/past-cancelled-revalidate.input.json
```

`verify_release.py` verifies every manifest hash and the pinned planner, runs the
complete synthetic suite, and executes every packaged example from a separate
temporary working directory. It makes no network or Calendar calls. Example
certificates are synthetic and must never be reused as evidence for real data.

Planner exit codes: 0 ready/bootstrap-ready/series-rebind-ready, 1 review required, 2 blocked. Adapter
exit 0 means normalized input is valid; exit 2 blocks. Preflight exits 0 only with
`ready_to_prepare:true`, while `allowed:false` always remains false. Final
Calendar revalidation exits 0 only with `allowed:true`. The separate series guard
exits 0 only with `state_write_allowed:true`; it always prohibits Calendar calls.
**Gate every command exit and
required JSON result before proceeding.** Neither a ready plan nor successful
preflight permits a Calendar call. Bootstrap and conflicts require their documented
resolution. Replan after each durable state/ledger change.

Confirmed cancellation or deletion of a known source now releases its unchanged
registered mirror even after the mirror ends. Each source must be re-read by exact
ID; age, a missing window result, an ambiguous 404 or an API error is never deletion
evidence. Ordinary past history remains. Manual non-time edits and manual changes
to an ended mirror's time remain conflicts. No per-marker cleanup approval field
or new input schema is required.

Upgrade the adapter and its pinned planner together. Raw schema 2, planner schema
3, RuntimeConfig 1 and ledger 2 are unchanged; preserve existing config, mappings,
raw occurrence identities and all ledger history. Pause/drain the old executor,
load the verified commit in cloud execution, reread both complete calendars and
all registered IDs, then replan and use the existing final write guard. This
repository does not perform that cutover. After confirmed deletion, verify the
destination terminal state, retire its mapping/issued entry while retaining its
known ID and operation history, and advance persisted generations. Unknown outcomes
hold for read-based reconciliation; never blindly retry a delete.

## Contracts

- [ADAPTER_CONTRACT.md](ADAPTER_CONTRACT.md): complete raw connector envelope,
  paging/detail certification, timezone handling and planner pinning.
- [PLANNER_CONTRACT.md](PLANNER_CONTRACT.md): deterministic normalized input/output,
  stable occurrence markers, safe updates/deletions and final reread contract.
- [RUNTIME_CONFIG.md](RUNTIME_CONFIG.md): required two-calendar scope, verified self
  identities and config-bound state/ledger. Start from `config.template.json`;
  `config.example.json` is synthetic only. No ambient accounts or defaults exist.
- [MANAGED_COVERAGE.md](MANAGED_COVERAGE.md): durable issued history, bounded
  observations, unbounded indexed queries and actual root-serialized execution.
- [RECOVERY_CONTRACT.md](RECOVERY_CONTRACT.md): zero-call execution evidence,
  single-use recovery, compact append-only refresh of unused certificates, scoped
  observation hashes and command/preflight gates.
- [SERIES_TRANSITION_CONTRACT.md](SERIES_TRANSITION_CONTRACT.md): reviewed weekly
  splits, including infinite continuations with complete current-window pages and
  preserved creation timestamps; stable occurrence IDs and unchanged mirrors;
  audited state-baseline replacement only, with a separate fresh guard.

An empty search never proves an uncertain write failed. Indexed searches cannot
detect an unknown manually obscured out-of-window copy. Pure JSON guards cannot
prove truthful external evidence, serialize writers, consume attempts or prevent
identical-input replay. The cloud root/executor must enforce admission and one-call
control flow and re-read all required observations. Manual read/write races remain
when the provider exposes no conditional writes.

The standalone `planner.py` also accepts already-normalized JSON. Public pure APIs
are `production_adapter.adapt(raw)`, `planner.plan(data)`,
`planner.preflight_action(action,fresh)`, `planner.revalidate_action(action,fresh)`,
`planner.revalidate_series_rebind(proposal,fresh)` and
`planner.recovery_observation_fingerprint(data)`. Production cutover and actual
Calendar operations are external responsibilities.

For new recovery approvals use `recovery_observation_fingerprint(data, marker)`
with `observations_scope:"recovery_item_v1"`. It binds relevant item/ownership,
coverage, state and ledger; unrelated native attachment changes do not invalidate
the target. The one-argument legacy algorithm remains unchanged. Refresh a stale
UNUSED certificate by appending its compact ID/hash-linked successor; never copy
large historical snapshots or refresh a consumed certificate. Close/drain the old
admission first. See RECOVERY_CONTRACT.md for the mandatory root audit and exact
record shape. Series identity changes remain conflicts unless the explicit narrow
state-only review contract passes. New/old iCalUID equality is not required.

GitHub is the distribution source: clone this repository and pin the verified
commit. Distribution ZIPs and Library ZIPs are unnecessary. After source/tests
change, `build_adapter_release.py` refreshes only the synthetic examples, test
results and manifest in this checkout; it does not create an archive or publish
anything. `verify_release.py` validates the exact checked-out source directly.

`distribution_check.py` scans explicit source file lists (and supports historical
archives when needed). A private deny-term JSON file may be supplied with
`--deny-json`; keep that file and private reports outside the repository. Pattern
scans cannot identify every unknown secret.
