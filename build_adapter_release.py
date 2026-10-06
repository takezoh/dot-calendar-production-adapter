"""Refresh synthetic tests/examples and the source-release manifest, offline."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

import production_adapter as adapter
import planner
import test_production_adapter as fixtures
import test_planner


def flatten(suite):
    for value in suite:
        if isinstance(value, unittest.TestSuite):
            yield from flatten(value)
        else:
            yield value


def main():
    root = Path(__file__).resolve().parent
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromModule(fixtures),
                               unittest.defaultTestLoader.loadTestsFromModule(test_planner)])
    ids = sorted(test.id() for test in flatten(suite))
    result = unittest.TestResult()
    suite.run(result)
    if not result.wasSuccessful():
        for test, error in result.errors + result.failures:
            print(test, error, file=sys.stderr)
        raise SystemExit(1)
    adapter.verify_planner()
    lines = [f"Production adapter {adapter.ADAPTER_VERSION} + planner {planner.RELEASE} synthetic test results",
             "Python: " + sys.version,
             f"Tests: {result.testsRun}; failures: {len(result.failures)}; errors: {len(result.errors)}; skipped: {len(result.skipped)}",
             "Timezone tests require real IANA zoneinfo data; no network/calendar/state writes occurred.",
             "", *["PASS " + name for name in ids]]
    (root / "TEST_RESULTS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    examples = root / "examples"
    examples.mkdir(exist_ok=True)
    source = fixtures.detail("demo-source")
    source.update(recurring_event_id="demo-series", original_start_time=source["start"])
    mirror = fixtures.target(source, event_id="demo-owned-mirror")
    stored = fixtures.mapping(source, mirror)
    create = fixtures.raw_input([source])
    fixtures.add_marker_lookup(create, source)
    changed = fixtures.detail(source["id"], "2026-10-09T18:00:00+09:00", "2026-10-09T19:00:00+09:00")
    changed.update(recurring_event_id=source["recurring_event_id"], original_start_time=source["original_start_time"])
    update = fixtures.raw_input([changed], [mirror], [stored])
    missing = fixtures.raw_input([], [mirror], [stored], extra_a=[fixtures.terminal(source["id"], "not_found")])
    all_day_source = fixtures.detail("demo-all-day", "2026-11-01", "2026-11-02")
    all_day = fixtures.raw_input([all_day_source], zones_a={all_day_source["id"]: fixtures.timezone_proof("America/New_York")})
    fixtures.add_marker_lookup(all_day, all_day_source)
    bootstrap = fixtures.raw_input([source], [mirror])
    fixtures.use_legacy(bootstrap, fixtures.legacy_registry(source, mirror))
    managed = fixtures.raw_input([source])
    fixtures.add_managed_lookup(managed, source)
    managed["managed_context"] = test_planner.serialized({"managed_context": managed["managed_context"]})["managed_context"]
    datasets = {"timed-create": create, "verified-update": update, "missing-source": missing,
                "all-day-dst": all_day, "legacy-bootstrap": bootstrap, "managed-create": managed}
    recovery_raw, _ = fixtures.RecoveryAdapterTests().raw_recovery()
    datasets["recovery"] = recovery_raw
    refreshed_raw, _ = fixtures.RecoveryAdapterTests().raw_supersession()
    datasets["recovery-refresh"] = refreshed_raw
    names = ["production_adapter.py", "test_production_adapter.py", "ADAPTER_CONTRACT.md", "planner.py",
             "test_planner.py", "MANAGED_COVERAGE.md", "PLANNER_CONTRACT.md", "RUNTIME_CONFIG.md", "config.example.json",
             "config.template.json", "distribution_check.py", "build_adapter_release.py", "TEST_RESULTS.txt",
             "RECOVERY_CONTRACT.md", "README.md", "verify_release.py"]
    for name, raw in datasets.items():
        normalized = adapter.adapt(raw)
        for kind, value in (("raw", raw), ("normalized", normalized), ("plan", planner.plan(normalized))):
            filename = f"examples/{name}.{kind}.json"
            (root / filename).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
            names.append(filename)
    action = fixtures.writes(planner.plan(adapter.adapt(managed)))[0]
    filename = "examples/managed-revalidate.input.json"
    (root / filename).write_text(json.dumps({"action": action, "fresh": test_planner.claimed_fresh(action)},
        ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    names.append(filename)
    for prefix, raw in (("recovery", recovery_raw), ("recovery-refresh", refreshed_raw)):
        action = fixtures.writes(planner.plan(adapter.adapt(raw)))[0]
        for suffix, fresh in (("preflight", test_planner.preflight_fresh(action)), ("revalidate", test_planner.claimed_fresh(action))):
            filename = "examples/" + prefix + "-" + suffix + ".input.json"
            (root / filename).write_text(json.dumps({"action": action, "fresh": fresh}, indent=2, sort_keys=True)
                + "\n", encoding="utf-8", newline="\n")
            names.append(filename)
    manifest = {"release": adapter.ADAPTER_VERSION, "raw_schema_version": adapter.RAW_SCHEMA_VERSION,
                "planner_release": planner.RELEASE, "planner_schema_version": planner.SCHEMA_VERSION,
                "pinned_planner_sha256": adapter.PINNED_PLANNER_SHA256,
                "config_version": 1, "managed_ledger_version": 2,
                "tests_passed": result.testsRun,
                "files": [{"path": name, "bytes": (root / name).stat().st_size,
                           "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()} for name in names]}
    (root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"tests_passed": result.testsRun, "adapter_sha256": manifest["files"][0]["sha256"],
                      "manifest_path": "MANIFEST.json", "source_files": len(names),
                      "manifest_sha256": hashlib.sha256((root / "MANIFEST.json").read_bytes()).hexdigest(),
                      "pinned_planner_sha256": adapter.PINNED_PLANNER_SHA256}, indent=2))


if __name__ == "__main__":
    main()
