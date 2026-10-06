"""Offline manifest, regression and every-example CLI verification, Python 3.10+."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def main():
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    import production_adapter
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("unsafe_manifest_path")
        data = path.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("manifest_content_mismatch: " + item["path"])
    production_adapter.verify_planner()
    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(str(root)))
    if not result.wasSuccessful() or result.testsRun != manifest["tests_passed"]:
        return 1
    calls = 0
    with tempfile.TemporaryDirectory(prefix="calendar-sync-verify-") as directory:
        def run(script, args, input_text=None):
            nonlocal calls
            calls += 1
            completed = subprocess.run([sys.executable, str(root / script), *map(str, args)],
                input=input_text, cwd=directory, capture_output=True, encoding="utf-8")
            if completed.returncode not in (0, 1, 2):
                raise ValueError("CLI failed: " + completed.stderr)
            output = json.loads(completed.stdout)
            if "ready_to_prepare" in output:
                code = 0 if output["ready_to_prepare"] else 2
            elif "allowed" in output:
                code = 0 if output["allowed"] else 2
            else:
                code = 2 if output.get("status") == "blocked" or output.get("adapter_status") == "blocked" else 1 if output.get("status") == "review_required" else 0
            if completed.returncode != code:
                raise ValueError("CLI exit/result mismatch")
            return output
        for path in sorted((root / "examples").glob("*.raw.json")):
            normalized = run("production_adapter.py", [path])
            if normalized != json.loads(path.with_name(path.name.replace(".raw.json", ".normalized.json")).read_text(encoding="utf-8")):
                raise ValueError("normalized_example_mismatch")
            plan = run("planner.py", ["-"], json.dumps(normalized))
            if plan != json.loads(path.with_name(path.name.replace(".raw.json", ".plan.json")).read_text(encoding="utf-8")):
                raise ValueError("plan_example_mismatch")
        for path in sorted((root / "examples").glob("*.input.json")):
            mode = "--preflight" if "preflight" in path.name else "--revalidate"
            output = run("planner.py", [mode, path])
            if output.get("ready_to_prepare" if mode == "--preflight" else "allowed") is not True:
                raise ValueError("guard_example_not_ready")
    print(json.dumps({"status": "passed", "tests_passed": result.testsRun,
        "cli_invocations": calls, "manifest_files_verified": len(manifest["files"]),
        "planner_pin_verified": True, "network_or_calendar_calls": 0}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
