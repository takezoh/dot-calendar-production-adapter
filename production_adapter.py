#!/usr/bin/env python3
"""Offline raw Google Calendar projection -> pinned planner 1.3.2 schema 3.

No authentication, network, calendar writes, state writes, clock reads or daemon.
All read/completeness/timezone certificates must come from the authenticated caller.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Managed cloud runtimes can omit the script directory from sys.path. Add only
# this package's own directory; never depend on a desktop installation path.
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))
import planner

ADAPTER_VERSION = "1.2.2"
RAW_SCHEMA_VERSION = 2
PINNED_PLANNER_SHA256 = '315c348362e156e77a014273377806e4a257c99356e7f67b8307ab21c6819de1'
CAPABILITIES = {"concurrency_mode": "snapshot_reread", "provider_etag": False,
                "conditional_writes": False, "field_profile": "google_calendar_projection"}
DETAIL_FIELDS = {"guests_can_modify", "locked", "id", "summary", "status", "organizer",
                 "description", "location", "color_id", "event_label_id", "start", "end",
                 "attendees", "url", "hangout_link", "event_type", "transparency", "visibility",
                 "attachments", "reminders", "recurrence", "recurring_event_id",
                 "original_start_time", "display_url", "display_title"}
SEARCH_FIELDS = {"id", "summary", "description", "status", "start", "end", "original_start_time",
                 "recurring_event_id", "transparency", "event_type", "location", "my_response_status"}
NAMED_FIELDS = {"id", "summary", "description", "status", "start", "end", "attendees",
                "hangout_link", "event_type", "transparency", "visibility", "location", "reminders",
                "recurrence", "recurring_event_id", "original_start_time"}
READ_ONLY_FIELDS = {"organizer", "url", "display_url", "display_title"}


class AdapterError(ValueError):
    """A stable reason code; never include event content or secrets in errors."""


def require(condition, code):
    if not condition:
        raise AdapterError(code)


def object_keys(value, keys, code):
    require(isinstance(value, dict) and set(value) == set(keys), code)


def verify_planner():
    require(hashlib.sha256(Path(planner.__file__).read_bytes()).hexdigest() == PINNED_PLANNER_SHA256,
            "pinned_planner_bytes_mismatch")
    require(planner.RELEASE == "1.3.2" and planner.SCHEMA_VERSION == 3, "pinned_planner_version_mismatch")


def nonempty_string(value):
    return isinstance(value, str) and bool(value)


def optional_text(value):
    require(value is None or isinstance(value, str), "invalid_optional_text")
    return "" if value is None else value


def original(value):
    try:
        return planner.original(value)
    except (ValueError, TypeError) as exc:
        raise AdapterError("invalid_original_occurrence_identity") from exc


def resolve_midnight(day_string, timezone_name):
    """Resolve a real, unambiguous local midnight with zoneinfo, independently."""
    try:
        local_date = planner.day(day_string)
        zone = ZoneInfo(timezone_name)
        local = datetime.combine(local_date, time.min)
        candidates = {}
        for fold in (0, 1):
            candidate = local.replace(tzinfo=zone, fold=fold)
            utc = candidate.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == local:
                candidates[utc] = candidate
        require(bool(candidates), "all_day_midnight_nonexistent")
        require(len(candidates) == 1, "all_day_midnight_ambiguous")
        resolved = next(iter(candidates.values())).isoformat()
        planner.instant(resolved)
        return resolved
    except (ZoneInfoNotFoundError, OSError) as exc:
        raise AdapterError("timezone_database_or_zone_unavailable") from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, AdapterError):
            raise
        raise AdapterError("invalid_all_day_timezone_or_boundary") from exc


def normalize_times(start, end, certificate=None):
    require(isinstance(start, str) and isinstance(end, str), "time_values_must_be_raw_strings")
    is_start_date = planner.DAY.fullmatch(start) is not None
    is_end_date = planner.DAY.fullmatch(end) is not None
    require(is_start_date == is_end_date, "mixed_all_day_and_timed_boundaries")
    if not is_start_date:
        require(planner.instant(start) < planner.instant(end), "nonpositive_event_duration")
        return {"dateTime": start}, {"dateTime": end}, None
    object_keys(certificate, {"time_zone", "verified", "evidence"}, "all_day_timezone_certificate_required")
    require(certificate["verified"] is True and nonempty_string(certificate["time_zone"])
            and nonempty_string(certificate["evidence"]), "all_day_timezone_not_verified")
    zone = certificate["time_zone"]
    first, last = resolve_midnight(start, zone), resolve_midnight(end, zone)
    require(planner.day(start) < planner.day(end) and planner.instant(first) < planner.instant(last), "nonpositive_all_day_span")
    return {"date": start, "timeZone": zone}, {"date": end, "timeZone": zone}, {"start": first, "end": last}


def normalize_event(config, calendar_id, raw, timezone_certificate=None):
    config = planner.validate_config(config)
    require(calendar_id in planner.calendar_ids(config), "calendar_not_allowed")
    require(isinstance(raw, dict) and DETAIL_FIELDS <= set(raw), "incomplete_detailed_projection")
    require(nonempty_string(raw["id"]), "detail_event_id_required")
    require(raw["recurrence"] is None or raw["recurrence"] == [], "recurrence_not_expanded")
    attendees = deepcopy(raw["attendees"])
    require(isinstance(attendees, list), "detailed_attendees_not_returned")
    for attendee in attendees:
        require(isinstance(attendee, dict) and "is_self" in attendee and "response_status" in attendee,
                "incomplete_attendee_projection")
        require(attendee["is_self"] is None or type(attendee["is_self"]) is bool, "invalid_attendee_is_self")
        attendee["is_self"] = attendee["is_self"] is True
        require(attendee["response_status"] in planner.RESPONSES - {"none"}, "invalid_attendee_response")
    attendees.sort(key=planner.canonical)
    self_responses = [item["response_status"] for item in attendees if item["is_self"]]
    require(len(self_responses) <= 1, "ambiguous_self_attendee")
    rem = raw["reminders"]
    require(isinstance(rem, dict) and {"use_default", "overrides"} <= set(rem)
            and "useDefault" not in rem, "incomplete_or_ambiguous_reminders_projection")
    require(type(rem["use_default"]) is bool and (rem["overrides"] is None or isinstance(rem["overrides"], list)),
            "invalid_reminders_projection")
    overrides = deepcopy(rem["overrides"] or [])
    require(all(isinstance(item, dict) for item in overrides), "invalid_reminder_override")
    overrides.sort(key=planner.canonical)
    reminders = {key: deepcopy(value) for key, value in rem.items()
                 if key not in {"use_default", "overrides"} and value is not None}
    reminders.update(useDefault=rem["use_default"], overrides=overrides)
    # Every extra NONNULL exposed property is retained, including false/0/[]/{}.
    # Only the explicit read-only/computed projection fields are excluded.
    others = {key: deepcopy(value) for key, value in raw.items()
              if key not in NAMED_FIELDS | READ_ONLY_FIELDS and value is not None}
    begin, end, resolved = normalize_times(raw["start"], raw["end"], timezone_certificate)
    event_type = deepcopy(planner.UNKNOWN) if raw["event_type"] is None else raw["event_type"]
    require(raw["hangout_link"] is None or isinstance(raw["hangout_link"], str), "invalid_hangout_link")
    out = {"id": raw["id"], "etag": None, "status": raw["status"],
           "original_start_time": original(raw["original_start_time"]), "recurring_event_id": raw["recurring_event_id"],
           "start": begin, "end": end, "all_day_bounds": resolved,
           "self_response": self_responses[0] if self_responses else "none", "fields_verified": True,
           "fields": {"summary": optional_text(raw["summary"]), "description": optional_text(raw["description"]),
                      "visibility": raw["visibility"], "transparency": raw["transparency"],
                      "eventType": event_type, "attendees": attendees, "location": raw["location"],
                      "conferenceData": deepcopy(planner.UNKNOWN), "reminders": reminders,
                      "hangout_link": raw["hangout_link"] or None, "recurrence": [],
                      "other": {"observed": others, "unobserved_provider_fields": True}}}
    planner.validate_event(out, CAPABILITIES, config=config, calendar_id=calendar_id)
    return out


def collect_pages(pages, complete):
    require(isinstance(pages, list), "pages_must_be_array")
    require(type(complete) is bool, "page_completeness_must_be_boolean")
    require(not complete or bool(pages), "complete_search_requires_at_least_one_page")
    expected = None
    tokens, ids, events = set(), set(), []
    for index, page in enumerate(pages):
        object_keys(page, {"request_page_token", "response"}, "invalid_raw_page_record")
        require(page["request_page_token"] == expected, "page_chain_gap_or_wrong_request_token")
        if index:
            require(nonempty_string(expected), "unexpected_page_after_final_page")
        response = page["response"]
        require(isinstance(response, dict) and {"events", "next_page_token"} <= set(response), "incomplete_search_response")
        require(isinstance(response["events"], list), "search_events_must_be_array")
        for event in response["events"]:
            require(isinstance(event, dict) and SEARCH_FIELDS <= set(event), "incomplete_search_event_projection")
            require(nonempty_string(event["id"]) and event["id"] not in ids, "duplicate_or_invalid_search_event_id")
            ids.add(event["id"])
            events.append(event)
        expected = response["next_page_token"]
        require(expected is None or nonempty_string(expected), "invalid_next_page_token")
        if expected is not None:
            require(expected not in tokens, "pagination_token_loop")
            tokens.add(expected)
        require(response.get("has_more") is not True or expected is not None, "contradictory_page_completion")
    require(not complete or expected is None, "unread_calendar_pages")
    return sorted(events, key=lambda item: item["id"]), expected


def compare_search_detail(search, raw_detail, normalized):
    for key in SEARCH_FIELDS - {"my_response_status"}:
        listed, detailed = search[key], raw_detail[key]
        if key in {"summary", "description"}:
            listed, detailed = optional_text(listed), optional_text(detailed)
        require(listed == detailed, "event_changed_between_search_and_detail")
    hinted = search["my_response_status"] or "none"
    require(hinted == normalized["self_response"], "self_response_changed_between_search_and_detail")


class Adapter:
    def __init__(self, raw):
        self.raw = deepcopy(raw)
        self.observations = {}
        self.responses = {}
        self.requested = set()
        self.timezones = {}

    def detail_batches(self, calendar_id, batches):
        require(isinstance(batches, list), "detail_batches_must_be_array")
        for batch in batches:
            object_keys(batch, {"requested_ids", "complete", "responses"}, "invalid_detail_batch")
            require(batch["complete"] is True, "incomplete_detail_batch")
            ids, responses = batch["requested_ids"], batch["responses"]
            require(isinstance(ids, list) and 1 <= len(ids) <= 10, "detail_batch_must_request_1_to_10_ids")
            require(all(nonempty_string(eid) for eid in ids) and len(ids) == len(set(ids)), "duplicate_or_invalid_detail_request")
            require(isinstance(responses, list), "detail_responses_must_be_array")
            for eid in ids:
                require((calendar_id, eid) not in self.requested, "duplicate_detail_request_across_batches")
                self.requested.add((calendar_id, eid))
            returned = set()
            for response in responses:
                object_keys(response, {"event_id", "outcome", "event", "evidence"}, "partial_detail_response")
                eid = response["event_id"]
                require(eid in ids and eid not in returned, "unexpected_or_duplicate_detail_response")
                returned.add(eid)
                outcome = response["outcome"]
                require(outcome in {"found", "cancelled", "deleted", "not_found", "error"}, "invalid_detail_outcome")
                normalized = None
                if outcome == "found":
                    event = response["event"]
                    require(isinstance(event, dict) and event.get("id") == eid and response["evidence"] is None,
                            "detail_id_or_evidence_mismatch")
                    normalized = normalize_event(self.config, calendar_id, event, self.timezones[calendar_id].get(eid))
                    self.observations[calendar_id, eid] = (event, normalized)
                else:
                    require(response["event"] is None, "terminal_response_must_not_have_partial_event")
                    if outcome in {"deleted", "cancelled"}:
                        proof = response["evidence"]
                        object_keys(proof, {"kind", "verified_known_id", "proof"}, "terminal_known_id_evidence_required")
                        require(proof["kind"] == outcome and proof["verified_known_id"] is True and nonempty_string(proof["proof"]),
                                "terminal_known_id_evidence_required")
                    else:
                        require(response["evidence"] is None, "ambiguous_outcome_must_not_claim_terminal_evidence")
                self.responses[calendar_id, eid] = {"calendar_id": calendar_id, "event_id": eid,
                    "outcome": outcome, "event": normalized, "evidence": deepcopy(response["evidence"])}
            require(returned == set(ids), "requested_detail_ids_not_returned")

    def normalize_candidates(self, calendar_id, candidates):
        output = []
        for listed in candidates:
            key = calendar_id, listed["id"]
            require(key in self.observations, "search_candidate_requires_successful_direct_detail_read")
            raw_detail, normalized = self.observations[key]
            compare_search_detail(listed, raw_detail, normalized)
            output.append(deepcopy(normalized))
        return output

    def state(self):
        spec = self.raw["state"]
        require(isinstance(spec, dict), "state_wrapper_required")
        if spec.get("mode") == "planner":
            object_keys(spec, {"mode", "value"}, "invalid_planner_state_wrapper")
            state = deepcopy(spec["value"])
            require(isinstance(state, dict) and state.get("status") == "verified"
                    and state.get("mappings_complete") is True, "supplied_planner_state_not_verified_and_complete")
            return state
        object_keys(spec, {"mode", "registry_json", "complete"}, "invalid_legacy_bootstrap_wrapper")
        require(spec["mode"] == "legacy_bootstrap" and spec["complete"] is True
                and isinstance(spec["registry_json"], str), "explicit_complete_legacy_bootstrap_required")
        registry_bytes = spec["registry_json"].encode("utf-8")
        registry_hash = hashlib.sha256(registry_bytes).hexdigest()
        registry = json.loads(spec["registry_json"], object_pairs_hook=planner.unique_object, parse_constant=planner.reject_constant)
        require(isinstance(registry, dict) and {"namespace", "phase", "owner", "errors", "updated_at", "config", "records"} <= set(registry),
                "incomplete_legacy_registry")
        require(registry["namespace"] == self.config["namespace"], "legacy_namespace_mismatch")
        require(registry.get("phase") == "active" and registry.get("owner") is None
                and registry.get("errors") == [], "legacy_registry_not_quiescent_and_verified")
        require(nonempty_string(registry.get("updated_at")), "legacy_registry_revision_required")
        aliases = registry["config"]["calendars"]
        require(isinstance(aliases, dict) and len(aliases) == 2, "legacy_calendar_config_invalid")
        calendar_ids = {tag: value["calendar_id"] for tag, value in aliases.items()}
        require(set(calendar_ids.values()) == set(planner.calendar_ids(self.config)), "legacy_calendar_allowlist_mismatch")
        records = registry["records"]
        require(isinstance(records, list), "legacy_records_must_be_array")
        mappings = []
        for row in records:
            require(isinstance(row, dict) and {"source", "destination", "source_event_id", "destination_event_id",
                    "source_original_start_time", "source_recurring_event_id", "start", "end", "all_day", "source_status",
                    "source_response_status", "state", "error", "marker", "key", "last_verified_at"} <= set(row), "incomplete_legacy_row")
            require(row["state"] == "verified" and row["error"] is None,
                    "legacy_row_not_verified")
            source_calendar, destination_calendar = calendar_ids[row["source"]], calendar_ids[row["destination"]]
            source_id, destination_id = row["source_event_id"], row["destination_event_id"]
            marker = planner.marker_for(self.config, source_calendar, source_id, row["source_original_start_time"], destination_calendar)
            require(row["marker"] == marker and row["key"] == marker[-65:-1], "legacy_marker_or_key_mismatch")
            require(nonempty_string(row["last_verified_at"]), "legacy_row_verification_time_required")
            require(type(row["all_day"]) is bool and row["all_day"] == bool(planner.DAY.fullmatch(row["start"])),
                    "legacy_all_day_flag_mismatch")
            begin, end, _ = normalize_times(row["start"], row["end"], self.timezones[source_calendar].get(source_id))
            current = self.observations.get((source_calendar, source_id))
            if current:
                require(current[1]["recurring_event_id"] == row["source_recurring_event_id"], "legacy_series_identity_changed")
            mappings.append({"source": {"calendar_id": source_calendar, "event_id": source_id,
                                         "original_start_time": row["source_original_start_time"]},
                             "marker": marker, "destination": {"calendar_id": destination_calendar, "event_id": destination_id},
                             "legacy": {"verified": True, "start": begin, "end": end, "status": row["source_status"],
                                        "self_response": row["source_response_status"] or "none",
                                        "evidence": "registry-sha256:" + registry_hash + ";verified-at:" + row["last_verified_at"]}})
        return {"status": "bootstrap", "config_fingerprint": planner.config_fingerprint(self.config), "generation": "legacy-sha256:" + registry_hash + ";updated-at:" + registry["updated_at"],
                "mappings_complete": True, "mappings": sorted(mappings, key=lambda item: item["marker"])}

    def run(self):
        verify_planner()
        raw = self.raw
        envelope_keys = {"adapter_schema_version", "config", "run_started_at", "calendars", "state"}
        require(isinstance(raw, dict) and set(raw) in (envelope_keys, envelope_keys | {"managed_context"}), "invalid_raw_envelope")
        require(type(raw["adapter_schema_version"]) is int and raw["adapter_schema_version"] == RAW_SCHEMA_VERSION,
                "unsupported_raw_schema")
        self.config = planner.validate_config(raw["config"])
        run_start = planner.instant(raw["run_started_at"])
        end = run_start + timedelta(days=90)
        require(isinstance(raw["calendars"], list) and len(raw["calendars"]) == 2, "both_allowed_calendars_required")
        by_id = {}
        for calendar in raw["calendars"]:
            object_keys(calendar, {"calendar_id", "listing", "detail_batches", "marker_searches", "all_day_timezones"}, "invalid_raw_calendar")
            cid = calendar["calendar_id"]
            require(cid in planner.calendar_ids(self.config) and cid not in by_id, "calendar_not_allowed_or_duplicate")
            require(isinstance(calendar["all_day_timezones"], dict), "all_day_timezones_must_be_object")
            self.timezones[cid] = calendar["all_day_timezones"]
            self.detail_batches(cid, calendar["detail_batches"])
            by_id[cid] = calendar
        calendars = []
        for cid in planner.calendar_ids(self.config):
            calendar = by_id[cid]
            listing = calendar["listing"]
            object_keys(listing, {"complete", "expanded", "includes_ongoing", "time_min", "time_max", "pages"}, "invalid_raw_listing")
            require(listing["complete"] is True and listing["expanded"] is True and listing["includes_ongoing"] is True,
                    "listing_completeness_or_expansion_not_certified")
            require(planner.instant(listing["time_min"]) == run_start and planner.instant(listing["time_max"]) == end,
                    "listing_window_must_match_actual_run_start_plus_90_days")
            candidates, _ = collect_pages(listing["pages"], True)
            searches, seen_markers = [], set()
            require(isinstance(calendar["marker_searches"], list), "marker_searches_must_be_array")
            for search in calendar["marker_searches"]:
                managed = isinstance(search, dict) and search.get("scope") == planner.MANAGED_SCOPE
                object_keys(search, {"marker", "scope", "complete", "queries"} if managed else
                            {"marker", "scope", "complete", "coverage_verified", "evidence", "pages"}, "invalid_raw_marker_search")
                require(isinstance(search["marker"], str) and planner.own_marker(self.config).fullmatch(search["marker"])
                        and search["marker"] not in seen_markers and search["scope"] in {planner.UNIVERSAL_SCOPE, planner.MANAGED_SCOPE},
                        "invalid_or_duplicate_marker_search")
                seen_markers.add(search["marker"])
                if managed:
                    require(search["complete"] is True and isinstance(search["queries"], list), "managed_queries_must_be_complete")
                    normalized_queries, merged = [], {}
                    for query in search["queries"]:
                        object_keys(query, {"kind", "q", "time_min", "time_max", "complete", "pages"}, "invalid_raw_indexed_query")
                        require(query["complete"] is True, "indexed_query_incomplete")
                        found, token = collect_pages(query["pages"], True)
                        normalized_queries.append({key: deepcopy(query[key]) for key in ("kind", "q", "time_min", "time_max", "complete")})
                        normalized_queries[-1]["next_page_token"] = token
                        for candidate in self.normalize_candidates(cid, found):
                            require(candidate["id"] not in merged or candidate == merged[candidate["id"]], "inconsistent_query_candidates")
                            merged[candidate["id"]] = candidate
                    normalized_search = {"marker": search["marker"], "scope": search["scope"], "complete": True,
                                         "next_page_token": None, "queries": sorted(normalized_queries, key=lambda item: item["kind"]),
                                         "events": [merged[key] for key in sorted(merged)]}
                    planner.validate_indexed_queries(self.config, normalized_search)
                    searches.append(normalized_search)
                    continue
                require(type(search["complete"]) is bool and type(search["coverage_verified"]) is bool,
                        "marker_search_certificates_must_be_booleans")
                require(not search["complete"] or search["coverage_verified"] is True and nonempty_string(search["evidence"]),
                        "complete_marker_lookup_requires_truthful_coverage_evidence")
                found, token = collect_pages(search["pages"], search["complete"])
                searches.append({"marker": search["marker"], "scope": search["scope"], "complete": search["complete"],
                                 "next_page_token": token, "events": self.normalize_candidates(cid, found)})
            calendars.append({"id": cid,
                "listing": {"complete": True, "next_page_token": None, "expanded": True, "includes_ongoing": True,
                            "time_min": listing["time_min"], "time_max": listing["time_max"],
                            "events": self.normalize_candidates(cid, candidates)},
                "marker_searches": sorted(searches, key=lambda item: item["marker"])})
        result = {"schema_version": planner.SCHEMA_VERSION, "config": deepcopy(self.config), "run_started_at": raw["run_started_at"],
                  "connector_capabilities": deepcopy(CAPABILITIES), "calendars": calendars,
                  "details": {"complete": True, "requested": [planner.ref(*key) for key in sorted(self.requested)],
                              "responses": [self.responses[key] for key in sorted(self.responses)]}, "state": self.state()}
        if "managed_context" in raw:
            result["managed_context"] = deepcopy(raw["managed_context"])
        # Validate the pinned contract without mutating the calendars or registry.
        planner.Planner(result).validate()
        return result


def adapt(raw):
    """Return schema-3 planner input or raise AdapterError; never partially succeed."""
    try:
        return Adapter(raw).run()
    except AdapterError:
        raise
    except planner.ContractError as exc:
        raise AdapterError("planner_contract:" + str(exc)) from exc
    except (KeyError, TypeError, ValueError, OSError, OverflowError, RecursionError) as exc:
        raise AdapterError("malformed_raw_input_or_unavailable_dependency") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="-", help="raw UTF-8 JSON file or stdin")
    parser.add_argument("-o", "--output", default="-", help="schema-3 output file or stdout")
    args = parser.parse_args(argv)
    code = 0
    try:
        if args.input == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.input).read_text(encoding="utf-8")
        raw = json.loads(text, object_pairs_hook=planner.unique_object, parse_constant=planner.reject_constant)
        result = adapt(raw)
    except (AdapterError, ValueError, OSError, RecursionError) as exc:
        result = {"adapter_status": "blocked", "reason": str(exc) if isinstance(exc, AdapterError) else "invalid_json_or_unreadable_input",
                  "planner_input": None}
        code = 2
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if args.output == "-":
        sys.stdout.write(serialized)
    else:
        Path(args.output).write_text(serialized, encoding="utf-8", newline="\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
