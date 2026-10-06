# Portable calendar synchronization planner and adapter

Adapter **1.2.1** pins planner **1.3.1**. Python 3.10+ standard library, with IANA
timezone data available to `zoneinfo` for all-day events. Linux normally supplies
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
```

`verify_release.py` verifies every manifest hash and the pinned planner, runs the
complete synthetic suite, and executes every packaged example from a separate
temporary working directory. It makes no network or Calendar calls. Example
certificates are synthetic and must never be reused as evidence for real data.

Planner exit codes: 0 ready/bootstrap-ready, 1 review required, 2 blocked. Adapter
exit 0 means normalized input is valid; exit 2 blocks. Preflight exits 0 only with
`ready_to_prepare:true`, while `allowed:false` always remains false. Final
revalidation exits 0 only with `allowed:true`. **Gate every command exit and
required JSON result before proceeding.** Neither a ready plan nor successful
preflight permits a Calendar call. Bootstrap and conflicts require their documented
resolution. Replan after each durable state/ledger change.

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
  append-preserving single-use recovery and command/preflight gates.

An empty search never proves an uncertain write failed. Indexed searches cannot
detect an unknown manually obscured out-of-window copy. Pure JSON guards cannot
prove truthful external evidence, serialize writers, consume attempts or prevent
identical-input replay. The cloud root/executor must enforce admission and one-call
control flow and re-read all required observations. Manual read/write races remain
when the provider exposes no conditional writes.

The standalone `planner.py` also accepts already-normalized JSON. Public pure APIs
are `production_adapter.adapt(raw)`, `planner.plan(data)`,
`planner.preflight_action(action,fresh)`, `planner.revalidate_action(action,fresh)`
and `planner.recovery_observation_fingerprint(data)`. Production cutover and actual
Calendar operations are external responsibilities.

`build_adapter_release.py` regenerates synthetic examples, test results, manifest
and a portable ZIP after the source/tests change. `distribution_check.py` scans
explicit file lists/ZIPs, including nested archives. A private deny-term JSON file
may be supplied with `--deny-json`; keep that file and private reports outside the
repository. Pattern scans cannot identify every unknown secret.
