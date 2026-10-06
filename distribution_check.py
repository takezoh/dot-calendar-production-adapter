"""Offline distribution scan. Optional private deny terms stay outside the package.

python distribution_check.py release.zip --deny-json /private/terms.json -o report.json
The JSON deny file is an array of strings. Reports never echo matched values.
This is a known-pattern check, not proof that arbitrary unknown secrets are absent.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

PATTERNS = {
    "local_user_path": re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)[A-Za-z0-9_.-]+[\\/]", re.I),
    "private_artifact_id": re.compile(r"\b(?:libfile_|libdir_|file_)[0-9a-f]{32}\b", re.I),
    "private_uuid": re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.I),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
}
EMAIL = re.compile(r"(?<![A-Za-z0-9_.+-])[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def scan(paths, deny_terms=()):
    findings, inventory = [], []
    archives = 0
    def inspect(name, data, depth=0):
        nonlocal archives
        inventory.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        texts = [name, data.decode("utf-8", errors="ignore"), data.decode("utf-16-le", errors="ignore"), data.decode("utf-16-be", errors="ignore")]
        for index, term in enumerate(deny_terms):
            if term and any(term.casefold() in text.casefold() for text in texts):
                findings.append({"path": name, "rule": "private_deny_term", "term_index": index})
        for rule, pattern in PATTERNS.items():
            if any(pattern.search(text) for text in texts):
                findings.append({"path": name, "rule": rule})
        if any(not match.group().casefold().endswith(".invalid") for text in texts for match in EMAIL.finditer(text)):
            findings.append({"path": name, "rule": "non_synthetic_email"})
        if zipfile.is_zipfile(io.BytesIO(data)):
            archives += 1
            if depth >= 8:
                findings.append({"path": name, "rule": "archive_depth_limit"})
                return
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                seen = set()
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    pure = PurePosixPath(member.filename)
                    if member.filename in seen or pure.is_absolute() or ".." in pure.parts or "\\" in member.filename:
                        findings.append({"path": name, "rule": "unsafe_or_duplicate_archive_path"})
                    seen.add(member.filename)
                    inspect(name + "!/" + member.filename, archive.read(member), depth + 1)
                manifests = [m for m in seen if m.endswith("/MANIFEST.json") or m == "MANIFEST.json"]
                for filename in manifests:
                    manifest = json.loads(archive.read(filename))
                    prefix = filename[:-len("MANIFEST.json")]
                    expected = {prefix + item["path"] for item in manifest["files"]} | {filename}
                    if expected != seen:
                        findings.append({"path": name, "rule": "manifest_inventory_mismatch"})
                    for item in manifest["files"]:
                        content = archive.read(prefix + item["path"])
                        if len(content) != item["bytes"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
                            findings.append({"path": name, "rule": "manifest_content_mismatch"})
    for path in paths:
        path = Path(path)
        inspect(path.name, path.read_bytes())
    return {"status": "passed" if not findings else "failed", "entries_inspected": len(inventory),
            "archives_inspected": archives, "private_deny_term_count": len(deny_terms),
            "generic_pattern_count": len(PATTERNS) + 1, "findings": findings,
            "inventory": inventory, "limitation": "Checks supplied deny terms and generic patterns; cannot recognize all unknown private data."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--deny-json")
    parser.add_argument("-o", "--output")
    args = parser.parse_args()
    terms = json.loads(Path(args.deny_json).read_text(encoding="utf-8")) if args.deny_json else []
    if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
        raise ValueError("deny_json_requires_string_array")
    result = scan(args.paths, terms)
    serialized = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(serialized, encoding="utf-8", newline="\n")
    else:
        print(serialized, end="")
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
