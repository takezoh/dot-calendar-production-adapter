#!/usr/bin/env python3
"""Offline, deterministic, fail-closed calendar synchronization planning.

No credentials, network, clock reads, calendar writes, or persistence writes.
See README.md for the adapter/executor contract. Python 3.10+ standard library.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 3
RELEASE = "1.4.0"
RECOVERY_ITEM_SCOPE = "recovery_item_v1"
UNIVERSAL_SCOPE = "exact_marker_all_destinations"
MANAGED_SCOPE = "managed_state_window_and_indexed_search"
UNKNOWN = {"unobserved": True}
UNOBSERVABLE = ("conferenceData",)
ASCII_SPACE = str.maketrans("", "", "\t\n\v\f\r ")
ANY_MARKER = re.compile(r"\[dot-block-sync:v1:[0-9a-f]{32}:[0-9a-f]{64}\]")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
RESPONSES = {"none", "accepted", "declined", "tentative", "needsAction"}
FIELD_KEYS = {"summary", "description", "visibility", "transparency", "eventType",
              "attendees", "location", "conferenceData", "reminders", "other", "hangout_link", "recurrence"}
EVENT_KEYS = {"id", "etag", "status", "original_start_time", "recurring_event_id",
              "start", "end", "all_day_bounds", "self_response", "fields_verified", "fields"}
MUTATIONS = {"create", "update", "delete"}


class ContractError(ValueError):
    """Invalid/incomplete observations must not produce mutations."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def validate_capabilities(capabilities):
    require(isinstance(capabilities, dict) and set(capabilities) == {
        "concurrency_mode", "provider_etag", "conditional_writes", "field_profile"}, "explicit_connector_capabilities_required")
    require(type(capabilities["provider_etag"]) is bool and type(capabilities["conditional_writes"]) is bool,
            "capabilities_require_booleans")
    require(capabilities["field_profile"] in {"full", "google_calendar_projection"}, "unknown_field_profile")
    mode = capabilities["concurrency_mode"]
    require(mode in {"snapshot_reread", "provider_etag"}, "unknown_concurrency_mode")
    require((mode == "provider_etag" and capabilities["provider_etag"] and capabilities["conditional_writes"])
            or (mode == "snapshot_reread" and not capabilities["conditional_writes"]), "contradictory_connector_capabilities")


def concurrency_notice(capabilities):
    return {"mode": capabilities["concurrency_mode"], "provider_cas_available": capabilities["conditional_writes"],
            "immediate_prewrite_reread_required": True, "atomic_cross_event_guarantee": False,
            "limitation": "Snapshots detect observed changes; without provider CAS a change after reread can race with the write.",
            "unobservable_provider_fields": ["conferenceData", "eventType_if_not_returned", "other_unreturned_provider_fields"]
            if capabilities["field_profile"] == "google_calendar_projection" else []}


def snapshot_fingerprint(snapshot):
    """Hash the complete canonical normalized observation, never a fabricated etag."""
    return digest(snapshot)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def validate_config(config):
    """Return canonical explicit deployment config; never read ambient settings."""
    require(isinstance(config, dict) and set(config) == {"version", "namespace", "calendars"}, "explicit_config_required")
    require(type(config["version"]) is int and config["version"] == 1, "unsupported_config_version")
    require(isinstance(config["namespace"], str) and re.fullmatch(r"[0-9a-f]{32}", config["namespace"]), "invalid_config_namespace")
    require(isinstance(config["calendars"], list) and len(config["calendars"]) == 2, "config_requires_exactly_two_calendars")
    rows, ids = [], set()
    for row in config["calendars"]:
        require(isinstance(row, dict) and set(row) == {"calendar_id", "self_identities", "self_identities_verified"}, "invalid_config_calendar")
        cid = row["calendar_id"]
        require(nonempty(cid) and cid == cid.strip() and not any(ch.isspace() or ord(ch) < 33 or 127 <= ord(ch) <= 159 for ch in cid)
                and cid.casefold() not in {"primary", "default", "personal"} and cid not in ids,
                "config_calendar_requires_distinct_explicit_id")
        identities = row["self_identities"]
        require(row["self_identities_verified"] is True and isinstance(identities, list) and bool(identities), "verified_self_identities_required")
        require(all(isinstance(email, str) and re.fullmatch(r"[^\s@]+@[^\s@]+", email) for email in identities), "invalid_self_identity")
        normalized = [email.casefold() for email in identities]
        require(len(normalized) == len(set(normalized)), "duplicate_self_identity")
        ids.add(cid)
        rows.append({"calendar_id": cid, "self_identities": sorted(normalized), "self_identities_verified": True})
    return {"version": 1, "namespace": config["namespace"], "calendars": sorted(rows, key=lambda row: row["calendar_id"])}


def config_fingerprint(config):
    return digest(validate_config(config))


def calendar_ids(config):
    return tuple(row["calendar_id"] for row in config["calendars"])


def own_marker(config):
    return re.compile(r"\[dot-block-sync:v1:" + re.escape(config["namespace"]) + r":[0-9a-f]{64}\]")


def validate_self_identity(config, calendar_id, attendee):
    require(calendar_id in calendar_ids(config), "calendar_not_allowed")
    identities = next(row["self_identities"] for row in config["calendars"] if row["calendar_id"] == calendar_id)
    email = attendee.get("email")
    matched = isinstance(email, str) and email.casefold() in identities
    if attendee["is_self"]:
        require(matched, "self_attendee_not_in_verified_config")
    elif matched:
        # Never reinterpret a connector flag, infer another account, or silently
        # overlook a possible self-decline when account context contradicts it.
        require(False, "configured_self_identity_contradicts_connector_flag")


def validate_managed_context(config, context):
    """Validate explicit external certificates; never infer a ledger or a lock."""
    require(isinstance(context, dict) and set(context) == {"ledger", "single_writer"}, "invalid_managed_context")
    writer = context["single_writer"]
    require(isinstance(writer, dict), "external_single_writer_required")
    if writer.get("mode") == "serialized_runner":
        require(set(writer) == {"mode", "token", "root_coordinated", "cutover_paused_and_drained", "only_one_execution_admitted"}
                and nonempty(writer["token"]) and writer["root_coordinated"] is True
                and writer["cutover_paused_and_drained"] is True and writer["only_one_execution_admitted"] is True,
                "root_serialized_execution_required")
    else:
        require(set(writer) in ({"externally_enforced", "token"}, {"mode", "externally_enforced", "token"})
                and writer.get("mode", "atomic_claim") == "atomic_claim"
                and writer["externally_enforced"] is True and nonempty(writer["token"]), "external_single_writer_required")
    ledger = context["ledger"]
    ledger_keys = {"version", "config_fingerprint", "status", "complete", "durable", "generation", "provenance", "issued", "operations"}
    require(isinstance(ledger, dict) and set(ledger) in (ledger_keys, ledger_keys | {"recoveries"}), "invalid_issued_ledger")
    require(ledger["config_fingerprint"] == config_fingerprint(config), "ledger_config_mismatch")
    require(type(ledger["version"]) is int and ledger["version"] == 2 and ledger["status"] == "verified"
            and ledger["complete"] is True and ledger["durable"] is True and nonempty(ledger["generation"]),
            "complete_verified_durable_ledger_required")
    provenance = ledger["provenance"]
    require(isinstance(provenance, dict) and set(provenance) == {"kind", "history_complete", "state_verified", "evidence"}
            and provenance["kind"] == "verified_history_and_state" and provenance["history_complete"] is True
            and provenance["state_verified"] is True and nonempty(provenance["evidence"]), "ledger_migration_evidence_required")
    require(isinstance(ledger["issued"], list) and isinstance(ledger["operations"], list), "invalid_ledger_arrays")
    issued, destinations, operation_ids = {}, set(), set()
    for entry in ledger["issued"]:
        require(isinstance(entry, dict) and set(entry) == {"marker", "destination_calendar_id", "destination_ids", "disposition"},
                "invalid_issued_entry")
        marker = entry["marker"]
        require(isinstance(marker, str) and own_marker(config).fullmatch(marker) and marker not in issued, "invalid_or_duplicate_issued_marker")
        require(entry["destination_calendar_id"] in calendar_ids(config) and entry["disposition"] in {"mapped", "retired", "unresolved"}
                and isinstance(entry["destination_ids"], list), "invalid_issued_destination")
        require(entry["disposition"] != "mapped" or len(entry["destination_ids"]) == 1, "mapped_ledger_requires_one_destination")
        for event_id in entry["destination_ids"]:
            key = pair_ref(config, ref(entry["destination_calendar_id"], event_id))
            require(key not in destinations, "duplicate_ledger_destination")
            destinations.add(key)
        issued[marker] = entry
    for operation in ledger["operations"]:
        operation_keys = {"operation_id", "marker", "status", "action_id"}
        require(isinstance(operation, dict) and set(operation) in (operation_keys, operation_keys | {"recovery_id"}), "invalid_ledger_operation")
        require(nonempty(operation["operation_id"]) and operation["operation_id"] not in operation_ids
                and operation["marker"] in issued, "operation_requires_unique_id_and_issued_marker")
        require(operation["status"] in {"committed", "verified_no_write", "prepared", "attempt_started", "uncertain", "aborted_before_call"}, "invalid_operation_status")
        require(operation["action_id"] is None or nonempty(operation["action_id"]), "invalid_operation_action_id")
        require(operation["status"] in {"committed", "verified_no_write"} or nonempty(operation["action_id"]), "unresolved_operation_requires_action_id")
        operation_ids.add(operation["operation_id"])
    require(set(issued) <= {op["marker"] for op in ledger["operations"]}, "issued_markers_require_operation_history")
    validate_recoveries(config, context, issued)
    return issued, destinations


def validate_recoveries(config, context, issued):
    """Validate append-preserving recovery records, never infer zero calls."""
    ledger = context["ledger"]
    recoveries = ledger.get("recoveries", [])
    require(isinstance(recoveries, list), "invalid_recovery_history")
    operations = {op["operation_id"]: op for op in ledger["operations"]}
    positions = {op["operation_id"]: index for index, op in enumerate(ledger["operations"])}
    by_id, resolved, latest = {}, set(), {}
    for recovery in recoveries:
        full_keys = {"recovery_id", "operation_id", "marker", "outcome",
                "operation_fingerprint", "prior_action", "config_fingerprint", "observations_fingerprint",
                "authorized_admission_token", "evidence", "consumed_by_operation_id"}
        compact_keys = {"recovery_id", "operation_id", "marker", "observations_scope", "observations_fingerprint",
                        "authorized_admission_token", "consumed_by_operation_id", "supersession"}
        require(isinstance(recovery, dict) and set(recovery) in (full_keys, full_keys | {"observations_scope"}, compact_keys),
                "invalid_recovery_record")
        require("observations_scope" not in recovery or recovery["observations_scope"] == RECOVERY_ITEM_SCOPE,
                "unknown_recovery_observations_scope")
        rid, oid, marker = recovery["recovery_id"], recovery["operation_id"], recovery["marker"]
        require(nonempty(rid) and rid not in by_id and oid in operations, "duplicate_or_unknown_recovery_identity")
        origin = recovery
        if "supersession" in recovery:
            audit = recovery["supersession"]
            require(isinstance(audit, dict) and set(audit) == {"recovery_id", "recovery_fingerprint", "reason",
                    "audit_reference", "audit_sha256", "root_authorized", "previous_admission_closed", "certificate_unused_verified"},
                    "invalid_recovery_supersession")
            previous = by_id.get(audit["recovery_id"])
            require(previous is not None and latest.get(oid) == previous["recovery_id"]
                    and previous["operation_id"] == oid and previous["marker"] == marker,
                    "supersession_requires_latest_same_operation_certificate")
            require(previous["consumed_by_operation_id"] is None, "consumed_recovery_cannot_be_superseded")
            require(audit["recovery_fingerprint"] == digest(previous), "superseded_recovery_snapshot_mismatch")
            require(audit["reason"] == "refresh_current_observations" and nonempty(audit["audit_reference"])
                    and isinstance(audit["audit_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", audit["audit_sha256"])
                    and audit["root_authorized"] is True and audit["previous_admission_closed"] is True
                    and audit["certificate_unused_verified"] is True, "audited_unused_supersession_required")
            require(recovery["authorized_admission_token"] not in {r["authorized_admission_token"]
                    for r in by_id.values() if r["operation_id"] == oid},
                    "supersession_requires_new_admission")
            origin = recovery_origin(by_id, previous)
        else:
            require(oid not in resolved, "duplicate_or_unknown_recovery_identity")
        op = operations[oid]
        require(marker == op["marker"] and marker in issued and origin["operation_fingerprint"] == digest(op), "recovery_operation_snapshot_mismatch")
        require(op["status"] in {"prepared", "attempt_started", "aborted_before_call", "verified_no_write"}
                and origin["outcome"] in {"aborted_before_call", "verified_no_write"}, "recovery_cannot_resolve_issued_or_uncertain_call")
        require(origin["config_fingerprint"] == config_fingerprint(config), "recovery_config_mismatch")
        require(isinstance(recovery["observations_fingerprint"], str) and re.fullmatch(r"[0-9a-f]{64}", recovery["observations_fingerprint"]), "invalid_recovery_observations_fingerprint")
        prior = origin["prior_action"]
        require(isinstance(prior, dict) and prior.get("id") == op["action_id"] == digest({k: v for k, v in prior.items() if k != "id"})
                and prior.get("op") == "create" and prior.get("marker") == marker, "recovery_prior_action_mismatch")
        expected = prior["expected"]
        require(config_fingerprint(expected["config"]) == origin["config_fingerprint"] == expected["state_config_fingerprint"], "recovery_prior_config_mismatch")
        for name in ("config", "source", "destination", "marker_inventory", "connector_capabilities", "managed_context"):
            require(digest(expected[name]) == expected["fingerprints"][name], "recovery_prior_fingerprint_mismatch")
        require(expected["source"]["outcome"] == "found" and expected["destination"]["outcome"] == "marker_absent"
                and expected["destination"]["event_id"] is None, "recovery_prior_action_was_not_absent_create")
        prior_writer = expected["managed_context"]["single_writer"]
        evidence = origin["evidence"]
        require(isinstance(evidence, dict) and set(evidence) == {"kind", "trace_reference", "trace_sha256", "prior_admission_token",
                "durable", "trace_complete", "execution_closed", "calendar_calls_issued", "call_dispatch_started", "external_write_uncertainty"},
                "recorded_zero_call_evidence_required")
        require(evidence["kind"] == "executor_trace" and nonempty(evidence["trace_reference"])
                and isinstance(evidence["trace_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", evidence["trace_sha256"])
                and evidence["durable"] is True and evidence["trace_complete"] is True and evidence["execution_closed"] is True
                and type(evidence["calendar_calls_issued"]) is int and evidence["calendar_calls_issued"] == 0
                and evidence["call_dispatch_started"] is False and evidence["external_write_uncertainty"] is False,
                "zero_external_calls_not_proven")
        require(prior_writer.get("mode") == "serialized_runner" and evidence["prior_admission_token"] == prior_writer["token"]
                and nonempty(recovery["authorized_admission_token"]) and recovery["authorized_admission_token"] != prior_writer["token"],
                "recovery_requires_new_serialized_admission")
        consumed = recovery["consumed_by_operation_id"]
        if consumed is not None:
            require(consumed in operations and positions[consumed] > positions[oid]
                    and operations[consumed].get("recovery_id") == rid and operations[consumed]["marker"] == marker
                    and operations[consumed]["action_id"] != op["action_id"], "recovery_consumption_mismatch")
        by_id[rid] = recovery
        resolved.add(oid)
        latest[oid] = rid
    for op in operations.values():
        if "recovery_id" in op:
            require(op["recovery_id"] in by_id and by_id[op["recovery_id"]]["consumed_by_operation_id"] == op["operation_id"], "operation_recovery_link_mismatch")
        if op["status"] == "aborted_before_call":
            require(op["operation_id"] in resolved, "aborted_before_call_requires_recorded_evidence")
    return by_id, resolved


def recovery_origin(records, recovery):
    """Resolve compact history references in memory without duplicating evidence."""
    seen = set()
    while "supersession" in recovery:
        key = recovery["supersession"]["recovery_id"]
        require(key not in seen and key in records, "invalid_recovery_reference_chain")
        seen.add(key)
        recovery = records[key]
    return recovery


def recovery_observation_fingerprint(data, marker=None):
    """With marker, bind relevant item/coverage/history; one-arg legacy stays exact."""
    if marker is not None:
        observer = Planner(data)
        observer.validate()  # Incomplete/contradictory inputs never get a scoped certificate.
        require(observer.managed is not None, "scoped_recovery_requires_managed_context")
        keys = [key for key, event in observer.observed.items()
                if description_kind(observer.config, event["fields"]["description"])[0] == "native"
                and marker_for(observer.config, *key, event["original_start_time"],
                    next(cid for cid in calendar_ids(observer.config) if cid != key[0])) == marker]
        require(len(keys) == 1, "scoped_recovery_requires_one_source_occurrence")
        key = keys[0]
        require(key in observer.details, "recovery_requires_current_source_by_id")
        destination_calendar = next(cid for cid in calendar_ids(observer.config) if cid != key[0])
        inventory = observer.marker_inventory(destination_calendar, marker, managed=True)
        matches = observer.markers.get((destination_calendar, marker), [])
        destination = observer.source_snapshot(matches[0]) if len(matches) == 1 else {
            "calendar_id": destination_calendar, "event_id": None, "outcome": "marker_absent", "event": None}
        return digest({"scope": RECOVERY_ITEM_SCOPE, "config": observer.config,
            "run_started_at": data["run_started_at"], "connector_capabilities": observer.capabilities,
            "state": data["state"], "source": observer.source_snapshot(key), "destination": destination,
            "marker_inventory": inventory, "source_occurrence_ambiguous": key in observer.ambiguous_sources,
            "coverage": {"calendars": [{"id": item["id"], "listing": {k: v for k, v in item["listing"].items() if k != "events"}}
                for item in sorted(data["calendars"], key=lambda c: c["id"])], "details_complete": data["details"]["complete"],
                "requested_detail_ids": [ref(*item) for item in sorted(observer.details)]},
            "ledger": {k: v for k, v in observer.managed["ledger"].items() if k != "recoveries"}})
    return digest({"config": validate_config(data["config"]), **{key: data[key] for key in
        ("run_started_at", "connector_capabilities", "calendars", "details", "state")}})


def active_recovery(config, context, marker):
    """Select one unused authorization; history is never cleared or replayed."""
    issued, _ = validate_managed_context(config, context)
    records, resolved = validate_recoveries(config, context, issued)
    superseded = {r["supersession"]["recovery_id"] for r in records.values() if "supersession" in r}
    candidates = [r for r in records.values() if r["marker"] == marker and r["consumed_by_operation_id"] is None
                  and r["recovery_id"] not in superseded]
    require(len(candidates) == 1, "issued_marker_requires_one_unused_zero_call_recovery")
    recovery = candidates[0]
    origin = recovery_origin(records, recovery)
    writer = context["single_writer"]
    require(writer.get("mode") == "serialized_runner" and writer["token"] == recovery["authorized_admission_token"],
            "recovery_admission_mismatch")
    entry = issued[marker]
    require(entry["disposition"] == "unresolved" and entry["destination_ids"] == []
            and entry["destination_calendar_id"] == origin["prior_action"]["destination"]["calendar_id"],
            "recovery_requires_no_known_destination")
    operations = [op for op in context["ledger"]["operations"] if op["marker"] == marker]
    require(operations[-1]["operation_id"] == recovery["operation_id"]
            and all(op["operation_id"] in resolved for op in operations), "recovery_has_unresolved_or_committed_history")
    prior = origin["prior_action"]["expected"]["managed_context"]["ledger"]
    # Preserve all history known by the failed action, including unrelated rows.
    require(context["ledger"]["operations"][:len(prior["operations"])] == prior["operations"]
            and context["ledger"]["issued"][:len(prior["issued"])] == prior["issued"], "recovery_prior_history_changed")
    for old in prior.get("recoveries", []):
        current = records.get(old["recovery_id"])
        require(current is not None and {k: v for k, v in current.items() if k != "consumed_by_operation_id"}
                == {k: v for k, v in old.items() if k != "consumed_by_operation_id"}, "recovery_prior_history_changed")
    return recovery


def validate_indexed_queries(config, search):
    require(isinstance(search["queries"], list) and len(search["queries"]) == 2, "namespace_and_key_queries_required")
    kinds = set()
    for query in search["queries"]:
        require(isinstance(query, dict) and set(query) == {"kind", "q", "time_min", "time_max", "complete", "next_page_token"},
                "invalid_indexed_query_certificate")
        kind = query["kind"]
        require(kind in {"namespace", "key"} and kind not in kinds, "namespace_and_key_queries_required")
        kinds.add(kind)
        require(query["q"] == (config["namespace"] if kind == "namespace" else search["marker"][-65:-1]), "indexed_query_term_mismatch")
        require(query["time_min"] is None and query["time_max"] is None, "indexed_queries_must_be_time_unbounded")
        require(query["complete"] is True and query["next_page_token"] is None, "indexed_query_has_unread_pages")


def instant(value):
    require(isinstance(value, str) and STAMP.fullmatch(value), "timestamp_requires_explicit_offset")
    if not value.endswith("Z"):
        require(int(value[-5:-3]) <= 23 and int(value[-2:]) <= 59 and not value.endswith("-00:00"),
                "invalid_or_unknown_utc_offset")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise ContractError("invalid_timestamp") from exc


def day(value):
    require(isinstance(value, str) and DAY.fullmatch(value), "invalid_date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError("invalid_date") from exc


def original(value):
    require(value is None or isinstance(value, str), "original_start_must_be_raw_string_or_null")
    if value is not None:
        day(value) if DAY.fullmatch(value) else instant(value)
    return value


def same_original(left, right):
    """Only known IDs can reuse a registry identity after equivalent reformatting."""
    if left == right:
        return True
    if left is None or right is None or DAY.fullmatch(left) or DAY.fullmatch(right):
        return False
    return instant(left) == instant(right)


def marker_for(config, source_calendar_id, source_event_id, source_original_start_time, destination_calendar_id):
    config = validate_config(config)
    require(source_calendar_id in calendar_ids(config) and destination_calendar_id in calendar_ids(config)
            and source_calendar_id != destination_calendar_id, "calendar_not_allowed")
    require(isinstance(source_event_id, str) and bool(source_event_id), "invalid_source_id")
    original(source_original_start_time)
    payload = ["dot-block-sync", 1, config["namespace"], source_calendar_id, source_event_id,
               source_original_start_time, destination_calendar_id]
    hashed = hashlib.sha256(compact(payload).encode("utf-8")).hexdigest()
    return f"[dot-block-sync:v1:{config['namespace']}:{hashed}]"


def description_kind(config, description):
    require(isinstance(description, str), "description_must_be_string")
    normalized = description.translate(ASCII_SPACE)
    if own_marker(config).fullmatch(normalized):
        return "owned", normalized
    if ANY_MARKER.fullmatch(normalized):
        return "foreign", normalized
    # Broad suspicion only; this never normalizes/adopts a malformed marker.
    if re.search(r"dot.*block.*sync", normalized, re.IGNORECASE | re.DOTALL):
        return "malformed", None
    return "native", None


def time_value(value):
    require(isinstance(value, dict), "time_must_be_object")
    require(set(value) <= {"date", "dateTime", "timeZone"}, "unknown_time_field")
    require(("date" in value) != ("dateTime" in value), "time_requires_date_xor_datetime")
    if "timeZone" in value:
        require(isinstance(value["timeZone"], str) and bool(value["timeZone"]), "invalid_timezone")
    if "date" in value:
        day(value["date"])
        require("timeZone" in value, "all_day_requires_timezone")
    else:
        instant(value["dateTime"])


def bounds(event):
    if "dateTime" in event["start"]:
        return instant(event["start"]["dateTime"]), instant(event["end"]["dateTime"])
    return instant(event["all_day_bounds"]["start"]), instant(event["all_day_bounds"]["end"])


def validate_event(event, capabilities=None, *, config, calendar_id):
    config = validate_config(config)
    require(calendar_id in calendar_ids(config), "calendar_not_allowed")
    require(isinstance(event, dict) and set(event) == EVENT_KEYS, "partial_or_unknown_event_fields")
    require(event["fields_verified"] is True, "unverified_event_fields")
    require(isinstance(event["id"], str) and bool(event["id"]), "event_requires_id")
    require(event["etag"] is None or isinstance(event["etag"], str) and bool(event["etag"]), "invalid_etag")
    if capabilities:
        require((capabilities["provider_etag"] and event["etag"] is not None)
                or (not capabilities["provider_etag"] and event["etag"] is None), "etag_does_not_match_declared_capability")
    require(event["status"] in {"confirmed", "tentative", "cancelled"}, "invalid_event_status")
    original(event["original_start_time"])
    require(event["recurring_event_id"] is None or
            (isinstance(event["recurring_event_id"], str) and bool(event["recurring_event_id"])),
            "invalid_recurring_event_id")
    require(event["recurring_event_id"] is None or event["original_start_time"] is not None,
            "recurrence_requires_original_identity")
    time_value(event["start"])
    time_value(event["end"])
    require(("date" in event["start"]) == ("date" in event["end"]), "mixed_date_and_datetime")
    if "date" in event["start"]:
        require(event["start"]["timeZone"] == event["end"]["timeZone"], "all_day_timezone_mismatch")
        require(isinstance(event["all_day_bounds"], dict)
                and set(event["all_day_bounds"]) == {"start", "end"}, "all_day_requires_resolved_bounds")
        for edge in ("start", "end"):
            raw = event["all_day_bounds"][edge]
            instant(raw)
            local = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            require(local.date() == day(event[edge]["date"])
                    and local.time().isoformat() == "00:00:00", "all_day_boundary_mismatch")
        require(day(event["start"]["date"]) < day(event["end"]["date"]), "invalid_all_day_span")
    else:
        require(event["all_day_bounds"] is None, "timed_event_has_all_day_bounds")
    begin, end = bounds(event)
    require(begin < end, "nonpositive_event_duration")
    require(event["self_response"] in RESPONSES, "invalid_self_response")
    fields = event["fields"]
    require(isinstance(fields, dict) and set(fields) == FIELD_KEYS, "partial_or_unknown_writable_fields")
    require(all(isinstance(fields[k], str) for k in ("summary", "description")), "invalid_text_fields")
    require(isinstance(fields["eventType"], str) or fields["eventType"] == UNKNOWN, "invalid_event_type")
    require(fields["visibility"] in {None, "default", "public", "private", "confidential"}, "invalid_visibility")
    require(fields["transparency"] in {None, "opaque", "transparent"}, "invalid_transparency")
    require(isinstance(fields["attendees"], list), "invalid_attendees")
    own_responses = []
    for attendee in fields["attendees"]:
        require(isinstance(attendee, dict) and isinstance(attendee.get("is_self"), bool)
                and attendee.get("response_status") in RESPONSES - {"none"}, "partial_attendee")
        validate_self_identity(config, calendar_id, attendee)
        if attendee["is_self"]:
            own_responses.append(attendee["response_status"])
    require(len(own_responses) <= 1, "ambiguous_self_attendee")
    require(event["self_response"] == (own_responses[0] if own_responses else "none"),
            "self_response_not_verified_from_attendees")
    require(fields["location"] is None or isinstance(fields["location"], str), "invalid_location")
    require(fields["conferenceData"] is None or isinstance(fields["conferenceData"], dict), "invalid_conference")
    require(isinstance(fields["reminders"], dict) and isinstance(fields["other"], dict), "invalid_extra_fields")
    require(not fields["other"].get("recurrence"), "unexpanded_recurrence")
    require(fields["hangout_link"] is None or isinstance(fields["hangout_link"], str), "invalid_hangout_link")
    require(isinstance(fields["recurrence"], list) and not fields["recurrence"], "unexpanded_recurrence")
    if capabilities:
        for key in UNOBSERVABLE:
            if capabilities["field_profile"] == "google_calendar_projection":
                require(fields[key] == UNKNOWN, "unobservable_field_must_not_be_invented")
            else:
                require(fields[key] != UNKNOWN, "full_profile_requires_observation")
        if capabilities["field_profile"] == "google_calendar_projection":
            require(set(fields["other"]) == {"observed", "unobserved_provider_fields"}
                    and isinstance(fields["other"]["observed"], dict)
                    and fields["other"]["unobserved_provider_fields"] is True, "projection_other_requires_observed_subset")
            require(not fields["other"]["observed"].get("recurrence"), "unexpanded_recurrence")
        else:
            require(fields["eventType"] != UNKNOWN, "full_profile_requires_observation")
            require(fields["other"] != UNKNOWN, "full_profile_requires_observation")


def mirror_fields(marker, profile="full"):
    fields = {"summary": "blocked", "description": marker, "visibility": "private",
            "transparency": "opaque", "eventType": "default", "attendees": [],
            "location": None, "conferenceData": None,
            "reminders": {"useDefault": False, "overrides": []}, "other": {}, "hangout_link": None, "recurrence": []}
    if profile == "google_calendar_projection":
        for key in UNOBSERVABLE:
            fields[key] = deepcopy(UNKNOWN)
        fields["eventType"] = deepcopy(UNKNOWN)
        fields["other"] = {"observed": {}, "unobserved_provider_fields": True}
    return fields


def protected(event):
    result = {"status": event["status"], "original_start_time": event["original_start_time"],
              "recurring_event_id": event["recurring_event_id"], "self_response": event["self_response"],
              "fields": deepcopy(event["fields"])}
    result["fields"]["description"] = result["fields"]["description"].translate(ASCII_SPACE)
    return result


def canonical_mirror(event, marker, profile="full"):
    expected_fields = mirror_fields(marker, profile)
    if profile == "google_calendar_projection" and event["fields"]["eventType"] != UNKNOWN:
        expected_fields["eventType"] = "default"
    return protected(event) == {"status": "confirmed", "original_start_time": None,
                               "recurring_event_id": None, "self_response": "none",
                               "fields": expected_fields}


def desired(source, marker):
    return {"status": "confirmed", "start": deepcopy(source["start"]), "end": deepcopy(source["end"]),
            "all_day_bounds": deepcopy(source["all_day_bounds"]), "fields": mirror_fields(marker)}


def timing(event):
    return {key: event[key] for key in ("start", "end", "all_day_bounds")}


def eligibility(event, run_start, window_end):
    if event["status"] == "cancelled":
        return "cancelled"
    if event["self_response"] == "declined":
        return "self_declined"
    if event["fields"]["transparency"] == "transparent":
        return "free"
    begin, end = bounds(event)
    if end <= run_start:
        return "ended_past"
    if begin >= window_end:
        return "outside_future_window"
    return "busy_in_window"


def ref(calendar_id, event_id):
    return {"calendar_id": calendar_id, "event_id": event_id}


def pair_ref(config, value):
    require(isinstance(value, dict) and set(value) == {"calendar_id", "event_id"}, "invalid_event_ref")
    require(value["calendar_id"] in calendar_ids(config) and isinstance(value["event_id"], str)
            and bool(value["event_id"]), "calendar_or_id_not_allowed")
    return value["calendar_id"], value["event_id"]


def found_snapshot(calendar_id, event):
    return {"calendar_id": calendar_id, "event_id": event["id"], "outcome": "found", "event": deepcopy(event), "evidence": None}


def series_owner(config, calendar_id, organizer):
    require(isinstance(organizer, dict) and set(organizer) == {"email", "is_self"}
            and organizer["is_self"] is True and nonempty(organizer["email"]), "series_self_ownership_not_verified")
    identities = next(c["self_identities"] for c in config["calendars"] if c["calendar_id"] == calendar_id)
    require(organizer["email"].casefold() in {email.casefold() for email in identities}, "series_owner_outside_verified_config")


def series_master(config, calendar_id, value):
    """Narrow timed weekly projection; full authenticated evidence stays external."""
    keys = {"calendar_id", "event_id", "status", "organizer", "created", "updated", "start", "end",
            "time_zone", "recurrence", "i_cal_uid"}
    require(isinstance(value, dict) and set(value) == keys and value["calendar_id"] == calendar_id
            and nonempty(value["event_id"]) and value["status"] == "confirmed" and nonempty(value["i_cal_uid"]),
            "invalid_series_master_projection")
    series_owner(config, calendar_id, value["organizer"])
    require(instant(value["created"]) <= instant(value["updated"]), "invalid_series_master_timestamps")
    require(instant(value["start"]) < instant(value["end"]) and nonempty(value["time_zone"]), "invalid_series_master_times")
    try:
        zone = ZoneInfo(value["time_zone"])
    except (ZoneInfoNotFoundError, OSError) as exc:
        raise ContractError("series_transition_timezone_unavailable") from exc
    local_start = instant(value["start"]).astimezone(zone)
    lines = value["recurrence"]
    require(isinstance(lines, list) and len(lines) == 1 and isinstance(lines[0], str) and lines[0].startswith("RRULE:"),
            "series_transition_requires_one_weekly_rrule")
    rule = {}
    for part in lines[0][6:].split(";"):
        pair = part.split("=")
        require(len(pair) == 2 and pair[0] not in rule and nonempty(pair[1]), "invalid_series_rrule")
        rule[pair[0]] = pair[1]
    weekday = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[local_start.weekday()]
    require(set(rule) <= {"FREQ", "INTERVAL", "BYDAY", "WKST", "UNTIL", "COUNT"}
            and rule.get("FREQ") == "WEEKLY" and rule.get("INTERVAL", "1") == "1"
            and rule.get("BYDAY", weekday) == weekday and rule.get("WKST", "MO") in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
            and ("UNTIL" in rule) != ("COUNT" in rule), "unsupported_series_transition_rrule")
    if "UNTIL" in rule:
        require(re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", rule["UNTIL"]), "series_until_requires_utc")
        try:
            limit = datetime.strptime(rule["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ContractError("invalid_series_until") from exc
        require(limit >= instant(value["start"]), "series_until_precedes_master_start")
    else:
        require(re.fullmatch(r"[1-9][0-9]*", rule["COUNT"]), "invalid_series_count")
        limit = None
    return {"zone": zone, "local_start": local_start, "duration": instant(value["end"]) - instant(value["start"]),
            "until": limit, "count": int(rule["COUNT"]) if "COUNT" in rule else None,
            "pattern": {"frequency": "WEEKLY", "weekday": weekday, "interval": 1, "week_start": rule.get("WKST", "MO")}}


def series_instance_pages(config, calendar_id, master, schedule, collection):
    keys = {"calendar_id", "master_id", "show_deleted", "time_min", "time_max", "complete", "pages"}
    require(isinstance(collection, dict) and set(collection) == keys and collection["calendar_id"] == calendar_id
            and collection["master_id"] == master["event_id"] and collection["show_deleted"] is True
            and collection["time_min"] is None and collection["time_max"] is None and collection["complete"] is True,
            "series_instances_require_complete_unbounded_show_deleted_reads")
    require(isinstance(collection["pages"], list) and bool(collection["pages"]), "series_instance_pages_missing")
    expected, tokens, rows, originals = None, set(), {}, set()
    for index, page in enumerate(collection["pages"]):
        require(isinstance(page, dict) and set(page) == {"request_page_token", "response"}
                and page["request_page_token"] == expected and (index == 0 or nonempty(expected)), "series_page_chain_gap")
        response = page["response"]
        require(isinstance(response, dict) and set(response) == {"events", "next_page_token"}
                and isinstance(response["events"], list), "invalid_series_page_response")
        for row in response["events"]:
            require(isinstance(row, dict) and set(row) == {"calendar_id", "event_id", "recurring_event_id", "original_start_time",
                    "status", "start", "end", "organizer", "i_cal_uid"}, "incomplete_series_instance_projection")
            require(row["calendar_id"] == calendar_id and row["recurring_event_id"] == master["event_id"]
                    and nonempty(row["event_id"]) and row["event_id"] not in rows and row["status"] == "confirmed"
                    and row["i_cal_uid"] == master["i_cal_uid"], "series_instance_identity_or_status_conflict")
            series_owner(config, calendar_id, row["organizer"])
            nominal = instant(row["original_start_time"])
            local = nominal.astimezone(schedule["zone"])
            require(nominal not in originals, "duplicate_series_original_occurrence")
            require(local.weekday() == schedule["local_start"].weekday()
                    and local.timetz().replace(tzinfo=None) == schedule["local_start"].timetz().replace(tzinfo=None)
                    and nominal >= instant(master["start"]), "series_instance_off_nominal_schedule")
            require(instant(row["start"]) < instant(row["end"]), "invalid_series_instance_times")
            require(schedule["until"] is None or nominal <= schedule["until"], "series_instance_after_until")
            rows[row["event_id"]] = row
            originals.add(nominal)
        expected = response["next_page_token"]
        require(expected is None or nonempty(expected) and expected not in tokens, "series_page_token_loop")
        if expected is not None:
            tokens.add(expected)
    require(expected is None, "series_instance_pages_incomplete")
    require(bool(rows) and (schedule["count"] is None or len(rows) == schedule["count"]), "series_instance_count_mismatch")
    # For this narrow single-weekday rule, an exhausted list must have every slot.
    local_dates = sorted(instant(r["original_start_time"]).astimezone(schedule["zone"]).date() for r in rows.values())
    require(local_dates[0] == schedule["local_start"].date()
            and all(right - left == timedelta(days=7) for left, right in zip(local_dates, local_dates[1:])),
            "series_instance_nominal_gap")
    if schedule["until"] is not None:
        last_local = max(originals).astimezone(schedule["zone"])
        require((last_local + timedelta(days=7)).astimezone(timezone.utc) > schedule["until"], "series_instances_omit_terminal_slot")
    return rows


EXECUTOR_REQUIREMENTS = [
    "Use only the existing authenticated Google Calendar plugin and the two explicit IDs and verified self identities in RuntimeConfig.",
    "Admit one root-coordinated serialized execution after paused/drained cutover, or use an actual external atomic-claim backend; owner fields and read/write readback are not locks.",
    "Immediately before every write reread source/destination, current-window inventory and registered destination IDs; call revalidate_action; replan on mismatch.",
    "Gate every command exit and result before the next journal or Calendar step. Use preflight_action before intent; check fingerprints before attempt_started whenever possible. Final revalidation must also pass.",
    "Only a complete durable zero-call execution trace plus explicit fresh serialized recovery approval permits one NEW linked attempt; preserve issued/operation history, never reopen or reuse the old attempt.",
    "Recheck whole-description marker and every observable protected field; never invent unexposed provider fields as absent.",
    "Before create require true universal lookup coverage or explicit managed state/window/indexed-search coverage; never equate exhausted q pages with universal absence.",
    "Managed creates require complete durable issuance/operation history, all tracked-ID reads, and durable prepared intent then attempt_started readback before one Calendar call; atomic claim is optional by capability.",
    "The stateless guard cannot detect identical-input replay. Serialized mode relies on one root-admitted uninterrupted execution, never resumes or retries an attempt, and holds crashes/unknown outcomes for read-based reconciliation.",
    "Managed indexed coverage cannot detect an unknown manually copied-and-obscured out-of-window mirror; inspect whole normalized candidate descriptions, never substring ownership.",
    "Use provider etag preconditions for updates/deletes when available; if unavailable, report the residual read/write race.",
    "Journal intent durably before write; after write, read and verify full fields then persist the verified mapping.",
    "On timeout/unknown outcome mark state uncertain, recover by reads of marker and known IDs, never blindly retry.",
    "Never execute conflict/noop; never apply normalization metadata such as all_day_bounds as API fields.",
    "Update only start/end; preserve all unspecified fields. Projection mode cannot prove absence of hidden provider properties.",
]


class Planner:
    def __init__(self, data):
        self.data = data
        self.observed = {}
        self.details = {}
        self.markers = {}
        self.suspects = {}
        self.searches = {}
        self.mappings = {}
        self.destinations = set()
        self.ambiguous_sources = set()
        self.actions = []
        self.candidates = []
        self.bootstrap_mappings = []
        self.handled = set()
        self.series_rebinds = []
        self.series_keys = set()

    def observe(self, calendar, event):
        validate_event(event, self.capabilities, config=self.config, calendar_id=calendar)
        key = calendar, event["id"]
        require(key not in self.observed or self.observed[key] == event, "inconsistent_event_snapshots")
        self.observed[key] = event

    def validate(self):
        data = self.data
        top_keys = {"schema_version", "config", "run_started_at", "calendars", "details", "state", "connector_capabilities"}
        require(isinstance(data, dict) and top_keys <= set(data) <= top_keys | {"managed_context", "series_transitions"},
                "invalid_top_level_contract")
        require(type(data["schema_version"]) is int and data["schema_version"] == SCHEMA_VERSION, "unsupported_schema")
        self.config = validate_config(data["config"])
        self.config_fingerprint = digest(self.config)
        validate_capabilities(data["connector_capabilities"])
        self.capabilities = data["connector_capabilities"]
        self.profile = self.capabilities["field_profile"]
        self.start = instant(data["run_started_at"])
        self.end = self.start + timedelta(days=90)
        require(isinstance(data["calendars"], list) and len(data["calendars"]) == 2, "both_calendars_required")
        seen_calendars = set()
        for calendar in data["calendars"]:
            require(isinstance(calendar, dict) and set(calendar) == {"id", "listing", "marker_searches"}, "invalid_calendar_contract")
            cid = calendar["id"]
            require(cid in calendar_ids(self.config) and cid not in seen_calendars, "calendar_not_allowed_or_duplicate")
            seen_calendars.add(cid)
            listing = calendar["listing"]
            require(isinstance(listing, dict) and set(listing) == {"complete", "next_page_token", "expanded",
                    "includes_ongoing", "time_min", "time_max", "events"}, "invalid_listing_contract")
            require(listing["complete"] is True and listing["next_page_token"] is None
                    and listing["expanded"] is True and listing["includes_ongoing"] is True, "incomplete_calendar_pages")
            require(instant(listing["time_min"]) == self.start and instant(listing["time_max"]) == self.end,
                    "window_must_be_run_start_plus_90_days")
            require(isinstance(calendar["marker_searches"], list), "invalid_marker_searches")
            for search in calendar["marker_searches"]:
                search_keys = {"marker", "complete", "next_page_token", "scope", "events"}
                require(isinstance(search, dict) and set(search) == (search_keys | {"queries"} if search.get("scope") == MANAGED_SCOPE else search_keys), "invalid_marker_search_contract")
                require(isinstance(search["marker"], str) and own_marker(self.config).fullmatch(search["marker"])
                        and search["scope"] in {UNIVERSAL_SCOPE, MANAGED_SCOPE} and type(search["complete"]) is bool,
                        "invalid_marker_search_certificate")
                if search["scope"] == MANAGED_SCOPE:
                    validate_indexed_queries(self.config, search)
                require((cid, search["marker"]) not in self.searches, "duplicate_marker_search")
                require(not search["complete"] or search["next_page_token"] is None, "marker_search_has_unread_pages")
                self.searches[cid, search["marker"]] = search
            for collection in [listing, *calendar["marker_searches"]]:
                require(isinstance(collection["events"], list), "events_must_be_array")
                seen = set()
                for event in collection["events"]:
                    self.observe(cid, event)
                    require(event["id"] not in seen, "duplicate_event_id_in_collection")
                    seen.add(event["id"])

        details = data["details"]
        require(isinstance(details, dict) and set(details) == {"complete", "requested", "responses"}
                and details["complete"] is True, "incomplete_details")
        require(isinstance(details["requested"], list) and isinstance(details["responses"], list), "invalid_details_arrays")
        requested = [pair_ref(self.config, item) for item in details["requested"]]
        require(len(set(requested)) == len(requested), "duplicate_detail_request")
        for response in details["responses"]:
            require(isinstance(response, dict) and set(response) == {"calendar_id", "event_id", "outcome", "event", "evidence"}, "partial_detail_response")
            key = pair_ref(self.config, ref(response["calendar_id"], response["event_id"]))
            require(key in requested and key not in self.details, "unexpected_or_duplicate_detail_response")
            outcome = response["outcome"]
            require(outcome in {"found", "cancelled", "deleted", "not_found", "error"}, "unknown_detail_outcome")
            if outcome == "found":
                require(response["event"] is not None and response["event"]["id"] == key[1], "detail_id_mismatch")
                self.observe(key[0], response["event"])
                require(response["evidence"] is None, "found_response_has_terminal_evidence")
            else:
                require(response["event"] is None, "terminal_detail_must_not_have_event")
                if outcome in {"cancelled", "deleted"}:
                    evidence = response["evidence"]
                    require(isinstance(evidence, dict) and set(evidence) == {"kind", "verified_known_id", "proof"}
                            and evidence["kind"] == outcome and evidence["verified_known_id"] is True
                            and isinstance(evidence["proof"], str) and bool(evidence["proof"]), "terminal_detail_requires_proof")
                else:
                    require(response["evidence"] is None, "ambiguous_detail_has_proof")
                require(key not in self.observed, "terminal_detail_contradicts_observation")
            self.details[key] = response
        require(set(requested) == set(self.details), "requested_detail_ids_not_returned")

        state = data["state"]
        require(isinstance(state, dict) and set(state) == {"status", "generation", "mappings", "mappings_complete", "config_fingerprint"}, "invalid_state_contract")
        require(state["config_fingerprint"] == self.config_fingerprint, "state_config_mismatch")
        require(state["status"] in {"verified", "absent", "uncertain", "bootstrap"}, "invalid_state_status")
        require(type(state["mappings_complete"]) is bool, "registry_completeness_certificate_required")
        require(state["status"] not in {"verified", "bootstrap"} or state["mappings_complete"] is True,
                "registry_not_complete")
        require(isinstance(state["generation"], str) and bool(state["generation"]), "state_generation_required")
        require(isinstance(state["mappings"], list), "mappings_must_be_array")
        for mapping in state["mappings"]:
            require(isinstance(mapping, dict) and (set(mapping) == {"source", "marker", "destination",
                    "verified_source", "verified_destination"} or set(mapping) == {"source", "marker", "destination", "legacy"}), "invalid_mapping_contract")
            require(("legacy" in mapping) == (state["status"] == "bootstrap"), "legacy_mappings_require_explicit_bootstrap_state")
            source = mapping["source"]
            require(isinstance(source, dict) and set(source) == {"calendar_id", "event_id", "original_start_time"}, "invalid_source_identity")
            skey = pair_ref(self.config, ref(source["calendar_id"], source["event_id"]))
            dkey = pair_ref(self.config, mapping["destination"])
            require(skey[0] != dkey[0], "same_calendar_mapping")
            require(mapping["marker"] == marker_for(self.config, *skey, source["original_start_time"], dkey[0]), "registry_marker_hash_mismatch")
            require(skey not in self.mappings and dkey not in self.destinations, "duplicate_registry_identity")
            if "legacy" in mapping:
                legacy = mapping["legacy"]
                require(isinstance(legacy, dict) and set(legacy) == {"verified", "start", "end", "status", "self_response", "evidence"}
                        and legacy["verified"] is True and isinstance(legacy["evidence"], str) and bool(legacy["evidence"]), "legacy_verification_evidence_required")
                time_value(legacy["start"])
                time_value(legacy["end"])
                require(legacy["status"] in {"confirmed", "tentative"} and legacy["self_response"] in RESPONSES - {"declined"}, "invalid_legacy_source_status")
            else:
                for field, key in (("verified_source", skey), ("verified_destination", dkey)):
                    validate_event(mapping[field], self.capabilities, config=self.config, calendar_id=key[0])
                    require(mapping[field]["id"] == key[1], "registry_snapshot_id_mismatch")
                require(same_original(mapping["verified_source"]["original_start_time"], source["original_start_time"]), "registry_original_identity_mismatch")
                require(canonical_mirror(mapping["verified_destination"], mapping["marker"], self.profile), "registry_baseline_not_canonical_mirror")
                require(description_kind(self.config, mapping["verified_source"]["fields"]["description"])[0] == "native", "registry_source_is_mirror")
            self.mappings[skey] = mapping
            self.destinations.add(dkey)
        require(not (set(self.mappings) & self.destinations), "registry_contains_sync_loop")
        require((set(self.mappings) | self.destinations) <= set(self.details), "registered_ids_require_explicit_detail_reads")
        self.managed = data.get("managed_context")
        self.issued, self.tracked = {}, set()
        if "managed_context" in data:
            self.issued, self.tracked = validate_managed_context(self.config, self.managed)
            require(self.tracked <= set(self.details), "ledger_destination_ids_require_explicit_detail_reads")
            require(not (self.tracked & set(self.mappings)), "ledger_destination_cannot_be_source")
        occurrence_ids = {}
        identity_observations = [(key, item["verified_source"]) for key, item in self.mappings.items() if "verified_source" in item]
        identity_observations.extend(self.observed.items())
        for key, event in identity_observations:
            if key in self.destinations or event["recurring_event_id"] is None:
                continue
            raw = event["original_start_time"]
            token = ("date", raw) if DAY.fullmatch(raw) else ("instant", instant(raw).isoformat())
            identity = (key[0], event["recurring_event_id"], token)
            occurrence_ids.setdefault(identity, set()).add(key)
        for keys in occurrence_ids.values():
            if len(keys) > 1:
                self.ambiguous_sources.update(keys)
        for key, event in self.observed.items():
            kind, marker = description_kind(self.config, event["fields"]["description"])
            if kind == "owned" and event["status"] != "cancelled":
                self.markers.setdefault((key[0], marker), []).append(key)
            elif kind == "malformed" and event["status"] != "cancelled":
                # Recognize an embedded/case-damaged key only to STOP its item,
                # never as ownership or permission to adopt the event.
                text = event["fields"]["description"].translate(ASCII_SPACE)
                for suspect in set(re.findall(own_marker(self.config).pattern, text, re.IGNORECASE)):
                    self.suspects.setdefault((key[0], suspect.lower()), []).append(key)
        if "series_transitions" in data:
            require(isinstance(data["series_transitions"], list) and bool(data["series_transitions"]), "series_transition_certificates_required")
            ids = set()
            for certificate in data["series_transitions"]:
                self.validate_series_transition(certificate)
                require(certificate["transition_id"] not in ids, "duplicate_series_transition_id")
                ids.add(certificate["transition_id"])

    def validate_series_transition(self, certificate):
        """Validate the whole split as one state-only adoption, never a Calendar edit."""
        keys = {"version", "kind", "transition_id", "config_fingerprint", "state_generation", "source_calendar_id",
                "split_original_start_time", "old_master", "new_master", "old_instances", "new_instances", "occurrences", "review"}
        require(isinstance(certificate, dict) and set(certificate) == keys and type(certificate["version"]) is int
                and certificate["version"] == 1 and certificate["kind"] == "following_events_split"
                and nonempty(certificate["transition_id"]), "invalid_series_transition_certificate")
        state = self.data["state"]
        require(state["status"] == "verified" and state["mappings_complete"] is True
                and certificate["state_generation"] == state["generation"]
                and certificate["config_fingerprint"] == self.config_fingerprint, "series_transition_state_or_config_mismatch")
        cid = certificate["source_calendar_id"]
        require(cid in calendar_ids(self.config), "series_transition_calendar_not_allowed")
        require(self.managed is not None and self.managed["single_writer"].get("mode") == "serialized_runner",
                "series_transition_requires_root_serialization")
        review = certificate["review"]
        require(isinstance(review, dict) and set(review) == {"status", "connector", "evidence_reference", "evidence_sha256",
                "readonly_evidence_verified", "previous_writers_drained", "admission_token", "timestamp_relation"}
                and review["status"] == "approved" and review["connector"] == "google_calendar_direct"
                and nonempty(review["evidence_reference"]) and isinstance(review["evidence_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}", review["evidence_sha256"])
                and review["readonly_evidence_verified"] is True and review["previous_writers_drained"] is True
                and review["admission_token"] == self.managed["single_writer"]["token"], "reviewed_series_transition_evidence_required")
        old, new = certificate["old_master"], certificate["new_master"]
        old_schedule, new_schedule = series_master(self.config, cid, old), series_master(self.config, cid, new)
        require(old["event_id"] != new["event_id"] and (cid, old["event_id"]) not in self.observed
                and (cid, new["event_id"]) not in self.observed, "series_masters_must_be_distinct_and_separate_from_expanded_events")
        require(review["timestamp_relation"] == "new_created_equals_updated"
                and instant(new["created"]) == instant(new["updated"])
                and instant(old["updated"]) <= instant(new["created"]), "series_creation_timestamp_evidence_mismatch")
        boundary = instant(certificate["split_original_start_time"])
        require(old_schedule["until"] is not None and old_schedule["until"] < boundary
                and instant(old["start"]) < boundary == instant(new["start"]), "series_split_boundary_not_proven")
        require(old["time_zone"] == new["time_zone"] and old_schedule["pattern"] == new_schedule["pattern"]
                and old_schedule["duration"] == new_schedule["duration"]
                and old_schedule["local_start"].timetz().replace(tzinfo=None) == new_schedule["local_start"].timetz().replace(tzinfo=None),
                "series_split_schedule_changed")
        old_rows = series_instance_pages(self.config, cid, old, old_schedule, certificate["old_instances"])
        new_rows = series_instance_pages(self.config, cid, new, new_schedule, certificate["new_instances"])
        for row in [*old_rows.values(), *new_rows.values()]:
            known_key = cid, row["event_id"]
            if known_key in self.details:
                require(self.details[known_key]["outcome"] == "found", "series_page_contradicts_known_id_detail")
            known = self.observed.get(known_key)
            if known is not None:
                require(known["status"] == row["status"] and known["recurring_event_id"] == row["recurring_event_id"]
                        and known["original_start_time"] == row["original_start_time"]
                        and known["start"].get("dateTime") == row["start"] and known["end"].get("dateTime") == row["end"],
                        "series_page_contradicts_known_id_detail")
        require(not set(old_rows) & set(new_rows)
                and all(instant(r["original_start_time"]) < boundary for r in old_rows.values())
                and all(instant(r["original_start_time"]) >= boundary for r in new_rows.values()), "series_split_instance_overlap")
        last_old = max(instant(r["original_start_time"]) for r in old_rows.values()).astimezone(old_schedule["zone"])
        require((last_old + timedelta(days=7)).astimezone(timezone.utc) == boundary, "series_split_nominal_boundary_gap")
        affected = {key for key, mapping in self.mappings.items() if key[0] == cid
                    and mapping["verified_source"]["recurring_event_id"] == old["event_id"]
                    and instant(mapping["source"]["original_start_time"]) >= boundary}
        require(affected and affected == {(cid, eid) for eid in new_rows} and not affected & self.series_keys,
                "series_transition_requires_exact_registered_occurrence_set")
        rows = certificate["occurrences"]
        require(isinstance(rows, list) and len(rows) == len(affected), "series_occurrence_certificates_incomplete")
        certified, replacements, snapshots = set(), [], []
        for item in rows:
            require(isinstance(item, dict) and set(item) == {"source_event_id", "original_start_time", "marker", "destination",
                    "mapping_fingerprint", "source_fingerprint", "destination_fingerprint"}, "invalid_series_occurrence_certificate")
            key = cid, item["source_event_id"]
            require(key in affected and key not in certified and key not in self.ambiguous_sources, "series_occurrence_ambiguous")
            certified.add(key)
            mapping, row = self.mappings[key], new_rows[key[1]]
            source = self.source_snapshot(key)
            dkey = pair_ref(self.config, mapping["destination"])
            destination = self.source_snapshot(dkey)
            current, target, baseline = source.get("event"), destination.get("event"), mapping["verified_source"]
            require(source["outcome"] == "found" and destination["outcome"] == "found"
                    and current is not None and target is not None, "series_rebind_requires_current_known_id_details")
            require(item["mapping_fingerprint"] == digest(mapping) and item["source_fingerprint"] == digest(source)
                    and item["destination_fingerprint"] == digest(destination), "series_occurrence_snapshot_mismatch")
            require(item["original_start_time"] == row["original_start_time"] == current["original_start_time"]
                    == baseline["original_start_time"] == mapping["source"]["original_start_time"]
                    and current["recurring_event_id"] == new["event_id"] and item["destination"] == mapping["destination"]
                    and item["marker"] == mapping["marker"], "series_stable_occurrence_identity_changed")
            require(current["status"] == baseline["status"] == "confirmed" and current["self_response"] == baseline["self_response"]
                    and eligibility(current, self.start, self.end) == "busy_in_window"
                    and description_kind(self.config, current["fields"]["description"])[0] == "native", "series_source_not_unchanged_busy")
            require("dateTime" in current["start"] and row["start"] == current["start"]["dateTime"]
                    and row["end"] == current["end"]["dateTime"]
                    and timing(current) == timing(baseline) == timing(target) == timing(mapping["verified_destination"]),
                    "series_rebind_requires_unchanged_source_and_mirror_times")
            require(self.markers.get((dkey[0], mapping["marker"]), []) == [dkey]
                    and not self.suspects.get((dkey[0], mapping["marker"]))
                    and canonical_mirror(target, mapping["marker"], self.profile)
                    and protected(target) == protected(mapping["verified_destination"]), "series_mirror_ownership_or_protected_fields_changed")
            entry = self.issued.get(mapping["marker"])
            require(entry is not None and entry["disposition"] == "mapped" and entry["destination_calendar_id"] == dkey[0]
                    and entry["destination_ids"] == [dkey[1]]
                    and any(op["marker"] == mapping["marker"] and op["status"] == "committed" for op in self.managed["ledger"]["operations"]),
                    "series_rebind_requires_verified_mapped_ledger")
            for event, uid in ((baseline, old["i_cal_uid"]), (current, new["i_cal_uid"])):
                other = event["fields"]["other"]
                exposed = other.get("observed", other)
                for name in ("iCalUID", "ical_uid", "i_cal_uid"):
                    require(name not in exposed or exposed[name] == uid, "series_ical_uid_projection_mismatch")
            replacement = deepcopy(mapping)
            replacement["verified_source"] = deepcopy(current)
            replacements.append(replacement)
            snapshots.append({"source": ref(*key), "source_fingerprint": digest(source), "destination": ref(*dkey),
                "destination_fingerprint": digest(destination), "mapping_fingerprint": digest(mapping),
                "marker_inventory_fingerprint": digest(self.marker_inventory(dkey[0], mapping["marker"], managed=True))})
        proposal = {"op": "rebind_series_state", "transition_id": certificate["transition_id"],
            "reason": "reviewed_following_events_split_with_stable_occurrences", "calendar_call_allowed": False,
            "expected": {"config_fingerprint": self.config_fingerprint, "state_generation": state["generation"],
                "state_fingerprint": digest(state), "managed_context_fingerprint": digest(self.managed),
                "connector_capabilities_fingerprint": digest(self.capabilities), "certificate_fingerprint": digest(certificate),
                "occurrences": sorted(snapshots, key=lambda row: row["source"]["event_id"])},
            "replacements": sorted(replacements, key=lambda mapping: mapping["marker"]),
            "audit": {"kind": "series_state_rebind", "old_master_id": old["event_id"], "new_master_id": new["event_id"],
                "evidence_reference": review["evidence_reference"], "evidence_sha256": review["evidence_sha256"],
                "only_mapping_verified_source_changes": True, "retain_all_marker_destination_and_operation_history": True}}
        proposal["id"] = digest(proposal)
        self.series_rebinds.append(proposal)
        self.series_keys.update(affected)

    def source_snapshot(self, key):
        if key in self.details:
            return deepcopy(self.details[key])
        event = self.observed.get(key)
        return found_snapshot(key[0], event) if event else {**ref(*key), "outcome": "unobserved", "event": None}

    def managed_create_blocker(self, marker, source, destination):
        if self.managed is None:
            return "managed_creation_requires_durable_ledger_and_single_writer"
        if self.data["state"]["status"] != "verified" or not self.data["state"]["mappings_complete"]:
            return "managed_creation_requires_complete_verified_state"
        recovery = None
        if marker in self.issued:
            if not self.managed["ledger"].get("recoveries"):
                return "previously_issued_marker_requires_verified_mapping"
            try:
                recovery = active_recovery(self.config, self.managed, marker)
                scope_marker = marker if recovery.get("observations_scope") == RECOVERY_ITEM_SCOPE else None
                require(recovery["observations_fingerprint"] == recovery_observation_fingerprint(self.data, scope_marker),
                        "recovery_current_observations_changed")
                require(pair_ref(self.config, ref(source["calendar_id"], source["event_id"])) in self.details,
                        "recovery_requires_current_source_by_id")
                prior = recovery_origin({r["recovery_id"]: r for r in self.managed["ledger"]["recoveries"]}, recovery)["prior_action"]["expected"]
                require(source == prior["source"] and destination == prior["destination"]
                        and self.capabilities == prior["connector_capabilities"], "recovery_source_or_destination_changed")
                search = self.searches.get((destination["calendar_id"], marker))
                require(search is not None and search["scope"] == MANAGED_SCOPE, "recovery_requires_current_indexed_queries")
                validate_indexed_queries(self.config, search)
            except ContractError as exc:
                return str(exc)
        by_marker = {item["marker"]: item for item in self.mappings.values()}
        if not set(by_marker) <= set(self.issued):
            return "ledger_omits_registered_issued_marker"
        _, resolved = validate_recoveries(self.config, self.managed, self.issued)
        if any(op["status"] not in {"committed", "verified_no_write"} and op["operation_id"] not in resolved
               for op in self.managed["ledger"]["operations"]):
            return "managed_ledger_has_unresolved_operations"
        for issued_marker, entry in self.issued.items():
            mapping = by_marker.get(issued_marker)
            if recovery is not None and issued_marker == marker and mapping is None:
                continue
            if entry["disposition"] == "mapped":
                if not any(op["marker"] == issued_marker and op["status"] == "committed" for op in self.managed["ledger"]["operations"]):
                    return "mapped_marker_requires_committed_operation_history"
                if mapping is None or mapping["destination"] != ref(entry["destination_calendar_id"], entry["destination_ids"][0]):
                    return "issued_marker_without_verified_mapped_destination"
                target = self.details[pair_ref(self.config, mapping["destination"])].get("event")
                if target is None or not canonical_mirror(target, issued_marker, self.profile) or protected(target) != protected(mapping["verified_destination"]):
                    return "tracked_destination_missing_or_manually_changed"
            elif entry["disposition"] == "retired":
                if mapping is not None or any(self.details[(entry["destination_calendar_id"], eid)]["outcome"] not in {"deleted", "cancelled"}
                                              for eid in entry["destination_ids"]):
                    return "retired_destination_requires_terminal_known_id_verification"
            else:
                return "issued_marker_has_unresolved_destination"
        for key, event in self.observed.items():
            kind, _ = description_kind(self.config, event["fields"]["description"])
            if kind == "malformed":
                return "managed_inventory_contains_malformed_marker"
            if kind == "owned" and key not in self.destinations:
                return "managed_inventory_contains_untracked_owned_marker"
        return None

    def marker_inventory(self, destination_calendar, marker, managed=False):
        """One shared inventory projection for planning, approval and reread guards."""
        matches = self.markers.get((destination_calendar, marker), [])
        inventory = {"calendar_id": destination_calendar, "marker": marker,
            "scope": "current_window_plus_registered_ids", "complete": True,
            "window": {"start": self.start.isoformat(), "end_exclusive": self.end.isoformat()},
            "registered_destination_ids": [ref(*key) for key in sorted(self.destinations)],
            "registry_complete": self.data["state"]["mappings_complete"],
            "marker_search": deepcopy(self.searches.get((destination_calendar, marker))),
            "matching_events": [deepcopy(self.observed[key]) for key in sorted(matches)],
            "suspect_events": [deepcopy(self.observed[key]) for key in sorted(self.suspects.get((destination_calendar, marker), []))]}
        if managed:
            inventory["tracked_destination_observations"] = [deepcopy(self.details[key]) for key in sorted(self.tracked)]
            inventory["observed_marker_events"] = [found_snapshot(key[0], self.observed[key]) for key in sorted(self.observed)
                if description_kind(self.config, self.observed[key]["fields"]["description"])[0] in {"owned", "malformed"}]
        return inventory

    def action(self, op, reason, source=None, marker=None, destination=None, payload=None, notes=None):
        destination = destination or {"calendar_id": None, "event_id": None, "outcome": "not_applicable", "event": None}
        record = {"op": op, "reason": reason, "marker": marker, "source": deepcopy(source),
                  "destination": deepcopy(destination), "desired": deepcopy(payload), "notes": notes or [],
                  "expected": {"config": deepcopy(self.config), "state_config_fingerprint": self.config_fingerprint,
                               "state_generation": self.data["state"]["generation"],
                               "connector_capabilities": deepcopy(self.capabilities),
                               "source": deepcopy(source), "destination": deepcopy(destination)}}
        if marker and destination["calendar_id"]:
            search = self.searches.get((destination["calendar_id"], marker))
            managed = bool(op == "create" and search and (search["scope"] == MANAGED_SCOPE or self.managed is not None))
            record["expected"]["marker_inventory"] = self.marker_inventory(destination["calendar_id"], marker, managed)
            if managed:
                record["expected"]["managed_context"] = deepcopy(self.managed)
                if marker in self.issued:
                    record["expected"]["recovery"] = deepcopy(active_recovery(self.config, self.managed, marker))
                    record["reason"] = "verified_zero_call_recovery_new_attempt"
        record["expected"]["fingerprints"] = {
            name: snapshot_fingerprint(record["expected"].get(name))
            for name in ("source", "destination", "marker_inventory", "connector_capabilities", "config")
            + (("managed_context",) if "managed_context" in record["expected"] else ())
            + (("recovery",) if "recovery" in record["expected"] else ())}
        record["concurrency"] = concurrency_notice(self.capabilities)
        record["write_contract"] = {"preserve_unspecified_fields": True,
                                    "update_fields": ["start", "end"],
                                    "create_only_default_event_without_attendees_location_conference_or_reminders": True,
                                    "never_send_unobserved_sentinels_or_normalization_metadata": True}
        if "managed_context" in record["expected"]:
            mode = self.managed["single_writer"].get("mode", "atomic_claim")
            record["write_contract"]["creation_attempt_mode"] = mode
            if mode == "atomic_claim":
                record["write_contract"]["durable_intent_then_atomic_once_only_attempt_claim_required"] = True
            else:
                record["write_contract"]["durable_intent_and_attempt_started_readback_required"] = True
                record["write_contract"]["root_serialized_one_call_no_resume_required"] = True
            if search["scope"] == MANAGED_SCOPE:
                record["notes"].append("Indexed search is not universal: an unknown manually copied and obscured out-of-window mirror can remain undetected.")
        record["id"] = digest(record)
        self.actions.append(record)

    def bootstrap(self, key, mapping):
        source = self.source_snapshot(key)
        dkey = pair_ref(self.config, mapping["destination"])
        destination = self.source_snapshot(dkey)
        marker = mapping["marker"]
        self.handled.add(dkey)
        matches = self.markers.get((dkey[0], marker), [])
        self.handled.update(matches)
        current_source, current_target = source.get("event"), destination.get("event")
        reason = None
        if key in self.ambiguous_sources:
            reason = "ambiguous_occurrence_ids"
        elif len(matches) != 1 or matches[0] != dkey or self.suspects.get((dkey[0], marker)):
            reason = "bootstrap_marker_not_unique_at_registered_id"
        elif current_source is None or current_target is None:
            reason = "bootstrap_requires_both_known_id_live_readbacks"
        elif not same_original(current_source["original_start_time"], mapping["source"]["original_start_time"]):
            reason = "source_occurrence_identity_changed"
        elif description_kind(self.config, current_source["fields"]["description"])[0] != "native":
            reason = "registry_source_is_mirror"
        elif not canonical_mirror(current_target, marker, self.profile):
            reason = "bootstrap_observable_mirror_constraints_not_met"
        else:
            legacy = mapping["legacy"]
            if (any(current_source[edge] != legacy[edge] or current_target[edge] != legacy[edge]
                    for edge in ("start", "end"))
                    or current_source["status"] != legacy["status"]
                    or current_source["self_response"] != legacy["self_response"]
                    or current_source["fields"]["transparency"] == "transparent"):
                reason = "bootstrap_live_state_differs_from_legacy_evidence"
        if reason:
            self.action("conflict", reason, source, marker, destination)
            return
        self.bootstrap_mappings.append({"source": deepcopy(mapping["source"]), "marker": marker,
                                        "destination": deepcopy(mapping["destination"]),
                                        "verified_source": deepcopy(current_source), "verified_destination": deepcopy(current_target)})
        self.action("noop", "bootstrap_live_readback_matches_legacy", source, marker, destination,
                    notes=["New baselines are current readbacks; no historical full snapshots were invented."])

    def reconcile(self, key, mapping=None):
        source_snapshot = self.source_snapshot(key)
        event = source_snapshot.get("event")
        raw_original = mapping["source"]["original_start_time"] if mapping else event["original_start_time"]
        dest_calendar = calendar_ids(self.config)[1] if key[0] == calendar_ids(self.config)[0] else calendar_ids(self.config)[0]
        marker = marker_for(self.config, *key, raw_original, dest_calendar)
        matches = self.markers.get((dest_calendar, marker), [])
        self.handled.update(matches)
        dkey = pair_ref(self.config, mapping["destination"]) if mapping else None
        destination = self.source_snapshot(dkey) if dkey else {
            "calendar_id": dest_calendar, "event_id": None, "outcome": "marker_absent", "event": None}
        if not mapping and len(matches) == 1:
            destination = self.source_snapshot(matches[0])
        if dkey:
            self.handled.add(dkey)
        notes = []

        def emit(op, reason, payload=None):
            self.action(op, reason, source_snapshot, marker, destination, payload, notes)

        if key in self.ambiguous_sources:
            emit("conflict", "ambiguous_occurrence_ids")
            return
        if len(matches) > 1:
            emit("conflict", "duplicate_marker")
            return
        if self.suspects.get((dest_calendar, marker)):
            emit("conflict", "malformed_description_contains_matching_marker")
            return
        if event and mapping and not same_original(event["original_start_time"], raw_original):
            emit("conflict", "source_occurrence_identity_changed")
            return
        if event and mapping and event["recurring_event_id"] != mapping["verified_source"]["recurring_event_id"]:
            if key in self.series_keys:
                emit("noop", "reviewed_series_split_requires_state_rebind")
                return
            emit("conflict", "source_series_identity_changed")
            return
        if event and event["original_start_time"] != raw_original:
            notes.append("equivalent_original_start_format_changed_registry_string_preserved")
        if event and description_kind(self.config, event["fields"]["description"])[0] != "native":
            emit("conflict", "registered_source_became_sync_marker")
            return
        if source_snapshot["outcome"] in {"not_found", "error", "unobserved"}:
            emit("conflict", "source_missing_without_terminal_verification")
            return
        reason = eligibility(event, self.start, self.end) if event else source_snapshot["outcome"]
        if not mapping:
            search = self.searches.get((dest_calendar, marker))
            if (matches or reason == "busy_in_window") and not (search and search["complete"]):
                emit("conflict", "marker_search_not_certified_for_create_or_adoption")
                return
            if reason == "busy_in_window" and search and (search["scope"] == MANAGED_SCOPE or self.managed is not None):
                blocker = self.managed_create_blocker(marker, source_snapshot, destination)
                if blocker:
                    emit("conflict", blocker)
                    return
            if matches:
                target = self.observed[matches[0]]
                if (reason == "busy_in_window" and canonical_mirror(target, marker, self.profile)
                        and timing(target) == timing(event)):
                    self.candidates.append({"source": {**ref(*key), "original_start_time": raw_original},
                                            "marker": marker, "destination": ref(*matches[0]),
                                            "verified_source": deepcopy(event), "verified_destination": deepcopy(target)})
                emit("conflict", "unregistered_mirror_requires_reconstruction")
            elif reason == "busy_in_window":
                emit("create", "eligible_busy_source_without_owned_mirror", desired(event, marker))
            else:
                emit("noop", "source_" + reason)
            return

        if matches and matches[0] != dkey:
            emit("conflict", "marker_at_unregistered_destination_id")
            return
        target = destination.get("event")
        if target is None:
            # Even confirmed manual deletion is not permission to recreate it.
            emit("conflict", "registered_destination_missing_requires_reconciliation")
            return
        if target["status"] == "cancelled":
            emit("conflict", "registered_destination_cancelled_requires_reconciliation")
            return
        if description_kind(self.config, target["fields"]["description"]) != ("owned", marker):
            emit("conflict", "destination_marker_changed")
            return
        if protected(target) != protected(mapping["verified_destination"]):
            emit("conflict", "destination_manual_non_time_edit")
            return
        if reason == "busy_in_window":
            if timing(target) == timing(event):
                emit("noop", "already_synchronized")
            else:
                emit("update", "source_or_mirror_time_changed", desired(event, marker))
            return
        if bounds(target)[1] <= self.start:
            emit("noop", "ended_past_mirror_retained")
        elif reason == "ended_past":
            if (timing(event) != timing(mapping["verified_source"])
                    and bounds(mapping["verified_source"])[1] > self.start):
                emit("delete", "known_source_moved_before_window")
            else:
                emit("noop", "ended_past_source_retained_no_bulk_cleanup")
        elif reason in {"cancelled", "deleted", "self_declined", "free", "outside_future_window"}:
            # A mapping always requires its direct known-ID source detail above.
            emit("delete", "known_source_" + reason)
        else:
            emit("conflict", "source_ineligible_without_safe_delete_evidence")

    def run(self):
        self.validate()
        for key, mapping in sorted(self.mappings.items()):
            self.bootstrap(key, mapping) if "legacy" in mapping else self.reconcile(key, mapping)
        for key, event in sorted(self.observed.items()):
            if key in self.mappings or key in self.destinations or key in self.tracked:
                continue
            kind, marker = description_kind(self.config, event["fields"]["description"])
            if kind == "native":
                if self.data["state"]["status"] == "bootstrap":
                    self.action("noop", "unregistered_native_deferred_during_bootstrap", source=self.source_snapshot(key))
                else:
                    self.reconcile(key)
            elif kind == "malformed":
                self.action("conflict", "malformed_or_extra_marker_text", destination=self.source_snapshot(key))
                self.handled.add(key)
            elif kind == "foreign":
                self.action("noop", "foreign_sync_mirror_excluded", destination=self.source_snapshot(key))
                self.handled.add(key)
        for key, event in sorted(self.observed.items()):
            if key in self.handled or key in self.destinations:
                continue
            kind, marker = description_kind(self.config, event["fields"]["description"])
            if kind == "owned":
                self.action("conflict", "orphan_owned_marker_requires_reconstruction", marker=marker,
                            destination=self.source_snapshot(key))
        status = self.data["state"]["status"]
        bootstrap_complete = False
        if status != "verified":
            for action in self.actions:
                if action["op"] in MUTATIONS:
                    action["op"] = "conflict"
                    action["notes"].append("withheld_operation_due_to_unverified_state")
                    action["reason"] = "state_" + status + "_requires_reconstruction"
                    action["desired"] = None
                    action.pop("id")
                    action["id"] = digest(action)
            bootstrap_complete = (status == "bootstrap" and bool(self.mappings)
                                  and len(self.bootstrap_mappings) == len(self.mappings)
                                  and not any(a["op"] == "conflict" for a in self.actions))
            if not bootstrap_complete:
                self.action("conflict", "state_" + status + "_requires_reconstruction")
        self.actions.sort(key=lambda item: item["id"])
        self.candidates.sort(key=lambda item: item["marker"])
        result = {"schema_version": SCHEMA_VERSION, "namespace": self.config["namespace"],
                  "config_fingerprint": self.config_fingerprint,
                  "status": "bootstrap_ready" if bootstrap_complete else ("blocked" if status != "verified" else ("review_required" if any(a["op"] == "conflict" for a in self.actions) else "ready")),
                  "run_started_at": self.data["run_started_at"],
                  "window": {"start": self.start.isoformat(), "end_exclusive": self.end.isoformat()},
                  "state_generation": self.data["state"]["generation"],
                  "actions": self.actions, "reconstruction_candidates": self.candidates,
                  "bootstrap_mappings": sorted(self.bootstrap_mappings, key=lambda item: item["marker"]),
                  "bootstrap_complete": bootstrap_complete,
                  "concurrency": concurrency_notice(self.capabilities),
                  "executor_requirements": EXECUTOR_REQUIREMENTS,
                  "counts": {op: sum(a["op"] == op for a in self.actions)
                             for op in ("create", "update", "delete", "noop", "conflict")}}
        if "series_transitions" in self.data:
            for action in self.actions:
                if action["op"] in MUTATIONS:
                    action.update(op="conflict", reason="calendar_mutations_deferred_during_series_transition_review", desired=None)
                    action.pop("id")
                    action["id"] = digest(action)
            self.actions.sort(key=lambda item: item["id"])
            conflict = any(action["op"] == "conflict" for action in self.actions)
            result["status"] = "review_required" if conflict else "series_rebind_ready"
            result["series_rebinds"] = [] if conflict else sorted(self.series_rebinds, key=lambda item: item["id"])
            result["calendar_call_allowed"] = False
            result["counts"] = {op: sum(action["op"] == op for action in self.actions)
                                for op in ("create", "update", "delete", "noop", "conflict")}
        result["plan_id"] = digest(result)
        return result


def plan(data):
    """Pure function. Any malformed/incomplete batch becomes a global conflict."""
    try:
        return Planner(deepcopy(data)).run()
    except (ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, ContractError) else "malformed_input"
        result = {"schema_version": SCHEMA_VERSION, "namespace": None, "config_fingerprint": None, "status": "blocked",
                  "actions": [{"op": "conflict", "reason": reason, "scope": "global",
                               "expected": {"source": None, "destination": None}, "desired": None}],
                  "reconstruction_candidates": [], "executor_requirements": EXECUTOR_REQUIREMENTS,
                  "bootstrap_mappings": [], "bootstrap_complete": False,
                  "counts": {"create": 0, "update": 0, "delete": 0, "noop": 0, "conflict": 1}}
        result["plan_id"] = digest(result)
        return result


REREAD_CERTIFICATE_KEYS = {"complete", "immediately_before_write", "source_by_id",
                          "destination_by_id_or_create_absence", "all_registered_destination_ids",
                          "current_window_pages_complete", "marker_lookup_repeated_when_required"}


def validate_creation_claim(action, fresh):
    """Check exact journal transition and the declared external admission mode.

    Serialized execution needs no CAS; this pure function cannot consume attempts.
    """
    before = action["expected"]["managed_context"]
    config = validate_config(fresh["config"])
    require(snapshot_fingerprint(before) == action["expected"]["fingerprints"]["managed_context"], "expected_managed_context_integrity_mismatch")
    issued, _ = validate_managed_context(config, before)
    recovery = None
    if action["marker"] in issued:
        recovery = active_recovery(config, before, action["marker"])
        require(action["expected"].get("recovery") == recovery
                and digest(recovery) == action["expected"]["fingerprints"].get("recovery"), "creation_recovery_snapshot_mismatch")
        prior = recovery_origin({r["recovery_id"]: r for r in before["ledger"]["recoveries"]}, recovery)["prior_action"]["expected"]
        require(all(action["expected"][key] == prior[key] for key in ("source", "destination", "connector_capabilities")),
                "recovery_source_or_destination_changed")
    else:
        require("recovery" not in action["expected"], "unexpected_creation_recovery")
    current = fresh["managed_context"]
    validate_managed_context(config, current)
    require(current["single_writer"] == before["single_writer"], "single_writer_token_changed")
    claim = fresh["creation_claim"]
    common_keys = {"operation_id", "action_id", "prepared_generation", "prepared_ledger_fingerprint",
                   "durable_intent_persisted", "from_status", "to_status"}
    mode = before["single_writer"].get("mode", "atomic_claim")
    if mode == "serialized_runner":
        serial_flags = {"intent_readback_verified", "attempt_started_readback_verified",
                        "same_uninterrupted_admitted_execution", "calendar_call_not_yet_attempted"}
        require(isinstance(claim, dict) and set(claim) == common_keys | serial_flags | {"mode", "resume_or_retry"},
                "invalid_serialized_creation_record")
        require(claim["mode"] == "serialized_runner" and all(claim[key] is True for key in serial_flags)
                and claim["resume_or_retry"] is False, "serialized_attempt_requires_readback_and_no_resume_or_retry")
    else:
        require(isinstance(claim, dict) and set(claim) == common_keys | {"atomic_once_only_claim"}, "invalid_creation_claim")
        require(claim["atomic_once_only_claim"] is True, "durable_once_only_attempt_claim_required")
    require(claim["durable_intent_persisted"] is True and claim["from_status"] == "prepared"
            and claim["to_status"] == "attempt_started", "durable_once_only_attempt_claim_required")
    require(claim["action_id"] == action["id"] and nonempty(claim["operation_id"])
            and claim["operation_id"] not in {op["operation_id"] for op in before["ledger"]["operations"]}, "creation_claim_not_bound_to_new_action")
    require(nonempty(claim["prepared_generation"]) and claim["prepared_generation"] != before["ledger"]["generation"]
            and current["ledger"]["generation"] not in {before["ledger"]["generation"], claim["prepared_generation"]},
            "journal_transition_requires_new_generations")
    prepared = deepcopy(before["ledger"])
    prepared["generation"] = claim["prepared_generation"]
    operation = {"operation_id": claim["operation_id"], "marker": action["marker"], "status": "prepared", "action_id": action["id"]}
    if recovery is None:
        prepared["issued"].append({"marker": action["marker"], "destination_calendar_id": action["destination"]["calendar_id"],
                                   "destination_ids": [], "disposition": "unresolved"})
    else:
        operation["recovery_id"] = recovery["recovery_id"]
        for record in prepared["recoveries"]:
            if record["recovery_id"] == recovery["recovery_id"]:
                record["consumed_by_operation_id"] = claim["operation_id"]
    prepared["operations"].append(operation)
    require(snapshot_fingerprint(prepared) == claim["prepared_ledger_fingerprint"], "durable_intent_snapshot_mismatch")
    prepared["generation"] = current["ledger"]["generation"]
    prepared["operations"][-1]["status"] = "attempt_started"
    require(prepared == current["ledger"], "issued_ledger_changed_beyond_claimed_intent")


def _revalidate_action(action, fresh, preflight=False):
    """Pure pre-write guard. Caller must actually perform the certified rereads.

    This is not a lock or CAS; allowed=True cannot prevent a subsequent race.
    """
    try:
        require(isinstance(action, dict) and action.get("op") in MUTATIONS, "action_is_not_a_mutation")
        require(action.get("id") == digest({k: v for k, v in action.items() if k != "id"}), "action_integrity_mismatch")
        managed = "managed_context" in action["expected"]
        fresh_keys = {"schema_version", "config", "state_config_fingerprint", "state_generation", "state_status", "connector_capabilities", "source",
                      "destination", "marker_inventory", "reread_certificate"}
        additions = ({"managed_context"} if preflight else {"managed_context", "creation_claim"}) if managed else set()
        require(isinstance(fresh, dict) and set(fresh) == fresh_keys | additions, "invalid_fresh_read_contract")
        require(type(fresh["schema_version"]) is int and fresh["schema_version"] == SCHEMA_VERSION
                and fresh["state_status"] == "verified", "fresh_state_not_verified")
        certificate = fresh["reread_certificate"]
        certificate_keys = REREAD_CERTIFICATE_KEYS | ({"all_ledger_destination_ids", "ledger_and_single_writer_reread"} if managed else set())
        require(isinstance(certificate, dict) and set(certificate) == certificate_keys
                and all(value is True for value in certificate.values()), "immediate_complete_rereads_required")
        expected = action["expected"]
        config = validate_config(fresh["config"])
        require(fresh["state_config_fingerprint"] == expected["state_config_fingerprint"] == config_fingerprint(config), "fresh_config_or_state_binding_mismatch")
        validate_capabilities(fresh["connector_capabilities"])
        require(fresh["state_generation"] == expected["state_generation"], "state_generation_changed")
        for name in ("source", "destination", "marker_inventory", "connector_capabilities", "config"):
            require(snapshot_fingerprint(expected.get(name)) == expected["fingerprints"][name], "expected_snapshot_integrity_mismatch")
            require(snapshot_fingerprint(config if name == "config" else fresh[name]) == expected["fingerprints"][name], name + "_snapshot_mismatch")
        for role in ("source", "destination"):
            observation = fresh[role]
            if observation and observation.get("calendar_id") is not None:
                require(observation["calendar_id"] in calendar_ids(config), "fresh_calendar_not_allowed")
            if observation and observation.get("event") is not None:
                validate_event(observation["event"], fresh["connector_capabilities"], config=config, calendar_id=observation["calendar_id"])
        inventory = fresh["marker_inventory"]
        require(inventory["complete"] is True and not inventory["suspect_events"], "marker_inventory_incomplete_or_suspect")
        if action["op"] == "create":
            search = inventory["marker_search"]
            require(search is not None and search["complete"] is True and search["next_page_token"] is None
                    and not inventory["matching_events"] and fresh["destination"]["outcome"] == "marker_absent", "creation_absence_not_verified")
            if search["scope"] == MANAGED_SCOPE:
                require(managed and inventory["registry_complete"] is True, "managed_creation_context_required")
                validate_indexed_queries(config, search)
            else:
                require(search["scope"] == UNIVERSAL_SCOPE, "unknown_creation_coverage")
            if managed:
                if preflight:
                    validate_managed_context(config, fresh["managed_context"])
                    require(fresh["managed_context"] == expected["managed_context"]
                            and digest(expected["managed_context"]) == expected["fingerprints"]["managed_context"],
                            "preflight_managed_context_changed")
                    if "recovery" in expected:
                        require(active_recovery(config, fresh["managed_context"], action["marker"]) == expected["recovery"]
                                and digest(expected["recovery"]) == expected["fingerprints"]["recovery"], "preflight_recovery_changed")
                else:
                    validate_creation_claim(action, fresh)
        else:
            target = fresh["destination"]["event"]
            require(target is not None and canonical_mirror(target, action["marker"], fresh["connector_capabilities"]["field_profile"])
                    and len(inventory["matching_events"]) == 1
                    and inventory["matching_events"][0]["id"] == target["id"], "destination_ownership_or_protected_fields_changed")
        etag = None
        if action["op"] != "create" and fresh["connector_capabilities"]["conditional_writes"]:
            etag = fresh["destination"]["event"]["etag"]
        if preflight:
            return {"allowed": False, "calendar_call_allowed": False, "ready_to_prepare": True,
                    "reason": "preflight_snapshots_match_no_write_authorization"}
        return {"allowed": True, "reason": "fresh_observable_snapshots_match",
                "destination_if_match_etag": etag, "concurrency": concurrency_notice(fresh["connector_capabilities"])}
    except (ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        result = {"allowed": False, "reason": str(exc) if isinstance(exc, ContractError) else "malformed_revalidation_input"}
        if preflight:
            result.update(calendar_call_allowed=False, ready_to_prepare=False)
        return result


def revalidate_action(action, fresh):
    """Final pure guard after durable intent and attempt-started readback."""
    return _revalidate_action(action, fresh)


def preflight_action(action, fresh):
    """Check current snapshots before journal preparation. Never permits a call."""
    return _revalidate_action(action, fresh, preflight=True)


def revalidate_series_rebind(proposal, fresh):
    """Pure state-write guard. Always prohibits Calendar calls."""
    result = {"allowed": False, "calendar_call_allowed": False, "state_write_allowed": False}
    try:
        require(isinstance(proposal, dict) and proposal.get("op") == "rebind_series_state"
                and proposal.get("id") == digest({k: v for k, v in proposal.items() if k != "id"}), "invalid_series_rebind_proposal")
        require(isinstance(fresh, dict) and set(fresh) == {"data", "reread_certificate"}, "invalid_series_rebind_fresh_contract")
        certificate = fresh["reread_certificate"]
        require(isinstance(certificate, dict) and set(certificate) == {"complete", "immediately_before_state_write",
                "source_and_destination_ids", "masters_and_instance_pages", "state_and_ledger", "single_writer"}
                and all(value is True for value in certificate.values()), "series_rebind_requires_immediate_complete_rereads")
        current = plan(fresh["data"])
        require(current["status"] == "series_rebind_ready" and proposal in current["series_rebinds"],
                "series_rebind_evidence_or_state_changed")
        result.update(state_write_allowed=True, reason="fresh_series_split_evidence_matches_state_only_proposal")
    except (ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ContractError) else "malformed_series_rebind_input"
    return result


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_json_object_key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ContractError("nonfinite_json_number")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="-", help="UTF-8 input JSON, or - for stdin")
    parser.add_argument("-o", "--output", default="-", help="output JSON, or - for stdout")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--revalidate", action="store_true", help="input is {action, fresh}; run mandatory offline pre-write guard")
    mode.add_argument("--preflight", action="store_true", help="check {action, fresh} before durable preparation; never authorize a Calendar call")
    mode.add_argument("--revalidate-series", action="store_true", help="check {proposal, fresh} for state-only series rebind; never authorize Calendar calls")
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            with open(args.input, encoding="utf-8") as handle:
                raw = handle.read()
        data = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
        if args.revalidate_series:
            require(isinstance(data, dict) and set(data) == {"proposal", "fresh"}, "invalid_series_revalidation_request")
            result = revalidate_series_rebind(data["proposal"], data["fresh"])
        elif args.revalidate or args.preflight:
            require(isinstance(data, dict) and set(data) == {"action", "fresh"}, "invalid_revalidation_request")
            result = (preflight_action if args.preflight else revalidate_action)(data["action"], data["fresh"])
        else:
            result = plan(data)
    except (OSError, ValueError, RecursionError):
        result = revalidate_series_rebind(None, None) if args.revalidate_series else preflight_action(None, None) if args.preflight else revalidate_action(None, None) if args.revalidate else plan(None)
    serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output == "-":
        sys.stdout.write(serialized)
    else:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
    if args.revalidate_series:
        return 0 if result["state_write_allowed"] else 2
    if args.preflight:
        return 0 if result["ready_to_prepare"] else 2
    if args.revalidate:
        return 0 if result["allowed"] else 2
    return 2 if result["status"] == "blocked" else (1 if result["status"] == "review_required" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
