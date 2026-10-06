"""Synthetic safety and compatibility tests. No external calendars or network."""
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import planner as p

A, B = "calendar-alpha@example.invalid", "calendar-beta@example.invalid"
CONFIG = {"version": 1, "namespace": "0123456789abcdef0123456789abcdef", "calendars": [
    {"calendar_id": A, "self_identities": [A], "self_identities_verified": True},
    {"calendar_id": B, "self_identities": [B], "self_identities_verified": True}]}
NAMESPACE = CONFIG["namespace"]
RUN = "2026-10-06T12:00:00Z"


def event(event_id="source-1", begin="2026-10-07T18:00:00+09:00", end="2026-10-07T19:00:00+09:00"):
    return {"id": event_id, "etag": '"v1-' + event_id + '"', "status": "confirmed",
            "original_start_time": None, "recurring_event_id": None,
            "start": {"dateTime": begin, "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": end, "timeZone": "Asia/Tokyo"},
            "all_day_bounds": None, "self_response": "none", "fields_verified": True,
            "fields": {"summary": "Synthetic meeting", "description": "",
                       "visibility": "default", "transparency": None, "eventType": "default",
                       "attendees": [], "location": None, "conferenceData": None,
                       "reminders": {"useDefault": True}, "other": {}, "hangout_link": None, "recurrence": []}}


def all_day(event_id="all-day", first="2026-10-07", last="2026-10-09", offsets=("+09:00", "+09:00"), zone="Asia/Tokyo"):
    result = event(event_id)
    result.update(start={"date": first, "timeZone": zone}, end={"date": last, "timeZone": zone},
                  all_day_bounds={"start": first + "T00:00:00" + offsets[0], "end": last + "T00:00:00" + offsets[1]})
    return result


def recurring(event_id="instance_20261007T090000Z", original="2026-10-07T18:00:00+09:00"):
    result = event(event_id)
    result.update(original_start_time=original, recurring_event_id="series-1")
    return result


def with_response(item, response):
    item["fields"]["attendees"] = [{"email": A, "is_self": True, "response_status": response}]
    item["self_response"] = response
    return item


def mirror(source, calendar=A, event_id="mirror-1", original=None):
    target = deepcopy(source)
    target.update(id=event_id, etag='"v1-' + event_id + '"', status="confirmed",
                  original_start_time=None, recurring_event_id=None, self_response="none")
    marker = p.marker_for(CONFIG, calendar, source["id"], source["original_start_time"] if original is None else original,
                          B if calendar == A else A)
    target["fields"] = p.mirror_fields(marker)
    return target


def found(calendar, item):
    return {"calendar_id": calendar, "event_id": item["id"], "outcome": "found", "event": deepcopy(item), "evidence": None}


def terminal(calendar, event_id, outcome):
    evidence = None if outcome in {"not_found", "error"} else {
        "kind": outcome, "verified_known_id": True, "proof": "synthetic-known-id-tombstone"}
    return {"calendar_id": calendar, "event_id": event_id, "outcome": outcome, "event": None, "evidence": evidence}


def registry(source, target, calendar=A):
    return {"source": {"calendar_id": calendar, "event_id": source["id"], "original_start_time": source["original_start_time"]},
            "marker": target["fields"]["description"].translate(p.ASCII_SPACE),
            "destination": {"calendar_id": B if calendar == A else A, "event_id": target["id"]},
            "verified_source": deepcopy(source), "verified_destination": deepcopy(target)}


def batch(a=None, b=None, mappings=None, status="verified", responses=None):
    mappings = deepcopy(mappings or [])
    calendars = []
    for calendar, items in ((A, a or []), (B, b or [])):
        calendars.append({"id": calendar,
                          "listing": {"complete": True, "next_page_token": None, "expanded": True,
                                      "includes_ongoing": True, "time_min": RUN,
                                      "time_max": (p.instant(RUN) + timedelta(days=90)).isoformat(), "events": deepcopy(items)},
                          "marker_searches": []})
    for index, source_items in ((0, a or []), (1, b or [])):
        for source_item in source_items:
            if p.description_kind(CONFIG, source_item["fields"]["description"])[0] != "native":
                continue
            # Some malformed-input tests intentionally remove identity fields.
            try:
                marker = p.marker_for(CONFIG, calendars[index]["id"], source_item["id"],
                                      source_item.get("original_start_time"), calendars[1-index]["id"])
            except (ValueError, TypeError):
                continue
            calendars[1-index]["marker_searches"].append({"marker": marker, "complete": True,
                "next_page_token": None, "scope": "exact_marker_all_destinations", "events": []})
    if responses is None:
        responses = []
        observed = {(cid, e["id"]): e for cid, items in ((A, a or []), (B, b or [])) for e in items}
        for mapping in mappings:
            for key in (mapping["source"], mapping["destination"]):
                pair = key["calendar_id"], key["event_id"]
                responses.append(found(pair[0], observed[pair]) if pair in observed else terminal(*pair, "not_found"))
    return {"schema_version": p.SCHEMA_VERSION, "config": deepcopy(CONFIG), "run_started_at": RUN,
            "connector_capabilities": {"concurrency_mode": "provider_etag", "provider_etag": True,
                                       "conditional_writes": True, "field_profile": "full"},
            "calendars": calendars,
            "details": {"complete": True, "requested": [p.ref(r["calendar_id"], r["event_id"]) for r in responses],
                        "responses": deepcopy(responses)},
            "state": {"config_fingerprint": p.config_fingerprint(CONFIG), "status": status, "generation": "synthetic-generation-1", "mappings": mappings,
                      "mappings_complete": status in {"verified", "bootstrap"}}}


def paired(source=None, target=None):
    source = source or event()
    target = target or mirror(source)
    return source, target, registry(source, target)


def writes(result):
    return [a for a in result["actions"] if a["op"] in p.MUTATIONS]


def projection(data):
    result = deepcopy(data)
    result["connector_capabilities"] = {"concurrency_mode": "snapshot_reread", "provider_etag": False,
                                       "conditional_writes": False, "field_profile": "google_calendar_projection"}
    def visit(value):
        if isinstance(value, dict):
            if set(value) == p.EVENT_KEYS:
                value["etag"] = None
                for field in p.UNOBSERVABLE:
                    value["fields"][field] = deepcopy(p.UNKNOWN)
                value["fields"]["eventType"] = deepcopy(p.UNKNOWN)
                value["fields"]["other"] = {"observed": deepcopy(value["fields"]["other"]), "unobserved_provider_fields": True}
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(result)
    return result


def legacy_bootstrap(data):
    result = projection(data)
    result["state"]["status"] = "bootstrap"
    for mapping in result["state"]["mappings"]:
        baseline = mapping.pop("verified_source")
        mapping.pop("verified_destination")
        mapping["legacy"] = {"verified": True, "start": baseline["start"], "end": baseline["end"],
                             "status": baseline["status"], "self_response": baseline["self_response"],
                             "evidence": "synthetic-prior-registry-verification"}
    return result


def fresh_for(action):
    return {"schema_version": p.SCHEMA_VERSION, "state_status": "verified",
            **{key: deepcopy(action["expected"][key]) for key in (
                "config", "state_config_fingerprint", "state_generation", "connector_capabilities", "source", "destination", "marker_inventory")},
            "reread_certificate": {key: True for key in p.REREAD_CERTIFICATE_KEYS}}


class PlanningTests(unittest.TestCase):
    def assert_reason(self, result, reason):
        self.assertIn(reason, [a["reason"] for a in result["actions"]], result)

    def assert_blocked(self, data, reason=None):
        result = p.plan(data)
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(writes(result), [])
        if reason:
            self.assert_reason(result, reason)
        return result

    def test_two_native_busy_events_get_bidirectional_mirrors_despite_overlap(self):
        result = p.plan(batch([event("a")], [event("b")]))
        self.assertEqual(result["counts"]["create"], 2)
        self.assertEqual({a["destination"]["calendar_id"] for a in writes(result)}, {A, B})

    def test_mirror_fields_are_private_minimal_and_do_not_copy_source_content(self):
        source = with_response(event(), "accepted")
        source["fields"].update(location="Sensitive place", conferenceData={"id": "secret"}, other={"attachments": ["secret"]})
        action = writes(p.plan(batch([source])))[0]
        fields = action["desired"]["fields"]
        self.assertEqual(fields, {"summary": "blocked", "description": action["marker"], "visibility": "private",
                                  "transparency": "opaque", "eventType": "default", "attendees": [],
                                  "location": None, "conferenceData": None,
                                  "reminders": {"useDefault": False, "overrides": []}, "other": {}, "hangout_link": None, "recurrence": []})
        self.assertEqual(action["expected"]["source"]["event"], source)
        self.assertEqual(action["expected"]["destination"]["outcome"], "marker_absent")

    def test_existing_mirrors_never_loop(self):
        a, b = event("native-a"), event("native-b")
        ma, mb = mirror(b, B, "mirror-a"), mirror(a, A, "mirror-b")
        result = p.plan(batch([a, ma], [b, mb], [registry(a, mb), registry(b, ma, B)]))
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 2, "conflict": 0})

    def test_registered_destination_with_removed_marker_cannot_become_source(self):
        source, target, mapping = paired()
        target["fields"]["description"] = "Manual edit removed marker"
        result = p.plan(batch([source], [target], [mapping]))
        self.assertEqual(writes(result), [])
        self.assertEqual(len(result["actions"]), 1)
        self.assert_reason(result, "destination_marker_changed")

    def test_golden_protocol_hash_from_independent_compact_json_dotnet_sha256(self):
        self.assertEqual(p.marker_for(CONFIG, A, "instance_20261007T090000Z", "2026-10-07T18:00:00+09:00", B),
                         "[dot-block-sync:v1:" + NAMESPACE + ":08722b2cad29a37a7b76c680f8b0158fd5140ed7570fab86d34d75ac08214fad]")

    def test_synthetic_279_pair_migration_preserves_all_raw_keys_and_destination_ids(self):
        by_calendar = {A: [], B: []}
        mappings = []
        for index in range(279):
            calendar = A if index % 2 == 0 else B
            destination_calendar = B if calendar == A else A
            begin = (p.instant("2026-10-07T09:00:00Z") + timedelta(hours=index)).isoformat()
            end = (p.instant(begin) + timedelta(minutes=30)).isoformat()
            source = event("synthetic-source-" + str(index), begin, end)
            if index < 277:
                source.update(original_start_time=begin, recurring_event_id="synthetic-series")
            target = mirror(source, calendar, "preserved-destination-" + str(index))
            mappings.append(registry(source, target, calendar))
            if source["original_start_time"]:
                source["original_start_time"] = source["original_start_time"].replace("+00:00", "Z")
            by_calendar[calendar].append(source)
            by_calendar[destination_calendar].append(target)
        result = p.plan(batch(by_calendar[A], by_calendar[B], mappings))
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 279, "conflict": 0})
        self.assertEqual({a["marker"] for a in result["actions"]}, {m["marker"] for m in mappings})
        self.assertEqual({a["destination"]["event_id"] for a in result["actions"]},
                         {m["destination"]["event_id"] for m in mappings})
        self.assertEqual(sum(bool(a["notes"]) for a in result["actions"]), 277)

    def test_null_and_raw_original_differ_and_never_use_current_start(self):
        source = event()
        first = writes(p.plan(batch([source])))[0]["marker"]
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        second = writes(p.plan(batch([source])))[0]["marker"]
        self.assertEqual(first, second)
        self.assertNotEqual(first, p.marker_for(CONFIG, A, source["id"], "2026-10-07T18:00:00+09:00", B))

    def test_recurrence_moved_exception_updates_same_destination_id(self):
        source, target, mapping = paired(recurring())
        source["start"]["dateTime"] = "2026-10-10T18:00:00+09:00"
        source["end"]["dateTime"] = "2026-10-10T20:00:00+09:00"
        result = p.plan(batch([source], [target], [mapping]))
        action = writes(result)[0]
        self.assertEqual(action["op"], "update")
        self.assertEqual(action["marker"], mapping["marker"])
        self.assertEqual(action["destination"]["event_id"], target["id"])

    def test_known_original_equivalent_format_preserves_registry_hash(self):
        source, target, mapping = paired(recurring())
        source["original_start_time"] = "2026-10-07T09:00:00Z"
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        result = p.plan(batch([source], [target], [mapping]))
        action = writes(result)[0]
        self.assertEqual(action["marker"], mapping["marker"])
        self.assertIn("equivalent_original_start_format_changed_registry_string_preserved", action["notes"])

    def test_original_different_instant_or_null_is_identity_conflict(self):
        for raw in ("2026-10-08T18:00:00+09:00", None):
            with self.subTest(raw=raw):
                source, target, mapping = paired(recurring())
                source["original_start_time"] = raw
                if raw is None:
                    source["recurring_event_id"] = None
                result = p.plan(batch([source], [target], [mapping]))
                self.assertEqual(writes(result), [])
                self.assert_reason(result, "source_occurrence_identity_changed")

    def test_series_id_change_is_not_silently_accepted(self):
        source, target, mapping = paired(recurring())
        source["recurring_event_id"] = "different-series"
        result = p.plan(batch([source], [target], [mapping]))
        self.assert_reason(result, "source_series_identity_changed")
        self.assertEqual(writes(result), [])

    def test_same_original_occurrence_returned_under_two_ids_is_ambiguous(self):
        first, second = recurring("first"), recurring("second", "2026-10-07T09:00:00Z")
        result = p.plan(batch([first, second]))
        self.assertEqual(writes(result), [])
        self.assertEqual(result["counts"]["conflict"], 2)
        self.assert_reason(result, "ambiguous_occurrence_ids")

    def test_missing_known_occurrence_and_reformatted_new_id_cannot_create_a_new_key(self):
        source, target, mapping = paired(recurring())
        alias = deepcopy(source)
        alias["id"] = "changed-id"
        alias["original_start_time"] = "2026-10-07T09:00:00Z"
        result = p.plan(batch([alias], [target], [mapping]))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "ambiguous_occurrence_ids")

    def test_distinct_occurrences_remain_distinct_when_their_times_overlap(self):
        first, second = recurring("instance-a"), recurring("instance-b", "2026-10-08T18:00:00+09:00")
        result = p.plan(batch([first, second]))
        self.assertEqual(result["counts"]["create"], 2)
        self.assertEqual(len({a["marker"] for a in writes(result)}), 2)

    def test_all_day_values_and_exclusive_date_end_preserved(self):
        source = all_day()
        source.update(original_start_time="2026-10-05", recurring_event_id="all-day-series")
        action = writes(p.plan(batch([source])))[0]
        self.assertEqual(action["desired"]["start"], source["start"])
        self.assertEqual(action["desired"]["end"], source["end"])
        self.assertEqual(action["desired"]["all_day_bounds"], source["all_day_bounds"])

    def test_all_day_dst_day_does_not_assume_24_hours(self):
        source = all_day(first="2026-11-01", last="2026-11-02", offsets=("-04:00", "-05:00"), zone="America/New_York")
        self.assertEqual(p.bounds(source)[1] - p.bounds(source)[0], timedelta(hours=25))
        self.assertEqual(p.plan(batch([source]))["counts"]["create"], 1)

    def test_all_day_moved_to_timed_is_same_occurrence(self):
        source, target, mapping = paired(all_day())
        moved = event(source["id"])
        result = p.plan(batch([moved], [target], [mapping]))
        self.assertEqual(writes(result)[0]["op"], "update")
        self.assertEqual(writes(result)[0]["marker"], mapping["marker"])

    def test_timed_offsets_are_preserved_verbatim(self):
        source = event(begin="2026-10-07T09:00:00-04:00", end="2026-10-07T10:00:00-04:00")
        source["start"]["timeZone"] = source["end"]["timeZone"] = "America/New_York"
        action = writes(p.plan(batch([source])))[0]
        self.assertEqual(action["desired"]["start"]["dateTime"], "2026-10-07T09:00:00-04:00")

    def test_no_timezone_database_required(self):
        with patch.dict("os.environ", {"PYTHONTZPATH": "nonexistent"}):
            self.assertEqual(p.plan(batch([all_day()]))["counts"]["create"], 1)

    def test_same_marker_duplicate_stops_item_but_not_independent_source(self):
        source, target, mapping = paired()
        duplicate = deepcopy(target)
        duplicate["id"] = "duplicate-mirror"
        result = p.plan(batch([source, event("independent")], [target, duplicate], [mapping]))
        self.assert_reason(result, "duplicate_marker")
        self.assertEqual(result["counts"]["create"], 1)
        self.assertEqual(writes(result)[0]["source"]["event_id"], "independent")

    def test_repeated_same_id_across_listing_inventory_detail_is_not_duplicate(self):
        source, target, mapping = paired()
        result = p.plan(batch([source], [target], [mapping]))
        self.assertEqual(result["counts"]["noop"], 1)
        self.assertEqual(result["counts"]["conflict"], 0)

    def test_all_ascii_whitespace_can_be_removed_and_only_ascii(self):
        source, target, mapping = paired()
        raw = target["fields"]["description"]
        target["fields"]["description"] = "\t\n\v\f\r " + " \t".join(raw) + "\r\n"
        self.assertEqual(p.plan(batch([source], [target], [mapping]))["counts"]["noop"], 1)
        for char in ("\u00a0", "\u2003", "\u200b"):
            with self.subTest(char=repr(char)):
                target["fields"]["description"] = char + raw
                result = p.plan(batch([source], [target], [mapping]))
                self.assertEqual(writes(result), [])
                self.assertGreater(result["counts"]["conflict"], 0)

    def test_extra_text_multiple_markers_uppercase_hash_and_short_hash_conflict(self):
        source, target, mapping = paired()
        raw = target["fields"]["description"]
        cases = [raw + " extra", raw + raw, raw[:-65] + raw[-65:-1].upper() + "]", raw[:-2] + "]"]
        for description in cases:
            with self.subTest(description=description):
                target["fields"]["description"] = description
                result = p.plan(batch([source], [target], [mapping]))
                self.assertEqual(writes(result), [])
                self.assertGreater(result["counts"]["conflict"], 0)

    def test_unregistered_extra_text_matching_marker_prevents_create(self):
        source = event()
        target = mirror(source)
        target["fields"]["description"] += " manual note"
        result = p.plan(batch([source], [target]))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "malformed_description_contains_matching_marker")

    def test_foreign_namespace_mirror_excluded_as_source(self):
        target = mirror(event())
        target["fields"]["description"] = target["fields"]["description"].replace(NAMESPACE, "a" * 32)
        result = p.plan(batch([target]))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "foreign_sync_mirror_excluded")

    def test_manual_blocked_is_preserved_and_can_be_a_native_busy_source(self):
        source = event("manual-block")
        source["fields"]["summary"] = "blocked"
        before = deepcopy(source)
        result = p.plan(batch([source]))
        self.assertEqual(result["counts"]["create"], 1)
        self.assertEqual(source, before)
        self.assertNotEqual(writes(result)[0]["destination"]["calendar_id"], A)

    def test_every_manual_non_time_field_is_protected(self):
        edits = {"summary": "Do not touch", "description": "Manual description", "visibility": "public",
                 "transparency": "transparent", "eventType": "focusTime", "location": "Room",
                 "conferenceData": {"id": "new"}, "reminders": {"useDefault": True},
                 "other": {"attachments": ["file"], "hangout_link": "https://example.invalid"},
                 "attendees": [{"is_self": False, "response_status": "needsAction"}]}
        for field, value in edits.items():
            with self.subTest(field=field):
                source, target, mapping = paired()
                target["fields"][field] = value
                source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
                result = p.plan(batch([source], [target], [mapping]))
                self.assertEqual(writes(result), [])
                self.assertGreater(result["counts"]["conflict"], 0)

    def test_manual_mirror_status_change_is_protected(self):
        source, target, mapping = paired()
        target["status"] = "tentative"
        result = p.plan(batch([source], [target], [mapping]))
        self.assert_reason(result, "destination_manual_non_time_edit")

    def test_time_only_mirror_edit_can_be_repaired(self):
        source, target, mapping = paired()
        target["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        action = writes(p.plan(batch([source], [target], [mapping])))[0]
        self.assertEqual(action["op"], "update")
        self.assertEqual(action["desired"]["start"], source["start"])

    def test_changed_metadata_etag_does_not_hide_manual_fields(self):
        source, target, mapping = paired()
        target["etag"] = '"new-etag"'
        result = p.plan(batch([source], [target], [mapping]))
        self.assertEqual(result["counts"]["noop"], 1)
        self.assertEqual(result["actions"][0]["expected"]["destination"]["event"]["etag"], '"new-etag"')

    def test_incomplete_pages_in_either_direction_blocks_all_mutations(self):
        for index in (0, 1):
            for field, value in (("complete", False), ("next_page_token", "more"), ("includes_ongoing", False), ("expanded", False)):
                with self.subTest(index=index, field=field):
                    data = batch([event()])
                    data["calendars"][index]["listing"][field] = value
                    self.assert_blocked(data, "incomplete_calendar_pages")

    def test_incomplete_marker_lookup_blocks_affected_create(self):
        data = batch([event()])
        data["calendars"][1]["marker_searches"][0]["complete"] = False
        result = p.plan(data)
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "marker_search_not_certified_for_create_or_adoption")

    def test_missing_requested_detail_response_blocks_batch(self):
        source, target, mapping = paired()
        data = batch([source], [target], [mapping])
        data["details"]["responses"].pop()
        self.assert_blocked(data, "requested_detail_ids_not_returned")

    def test_incomplete_details_flag_blocks_batch(self):
        data = batch([event()])
        data["details"]["complete"] = False
        self.assert_blocked(data, "incomplete_details")

    def test_registered_ids_must_be_requested_not_silently_omitted(self):
        source, target, mapping = paired()
        data = batch([source], [target], [mapping], responses=[])
        self.assert_blocked(data, "registered_ids_require_explicit_detail_reads")

    def test_partial_source_or_mirror_projection_blocks_batch(self):
        for key in ("fields_verified", "self_response", "etag", "original_start_time"):
            with self.subTest(key=key):
                source = event()
                del source[key]
                self.assert_blocked(batch([source]), "partial_or_unknown_event_fields")
        data = batch([event()])
        del data["calendars"][0]["listing"]["events"][0]["fields"]["conferenceData"]
        self.assert_blocked(data, "partial_or_unknown_writable_fields")

    def test_unknown_safety_fields_are_not_assumed_absent(self):
        data = batch([event()])
        data["calendars"][0]["listing"]["events"][0]["fields_verified"] = False
        self.assert_blocked(data, "unverified_event_fields")

    def test_personal_third_calendar_and_wrong_namespace_are_rejected(self):
        for location in ("calendar", "namespace", "mapping"):
            with self.subTest(location=location):
                source, target, mapping = paired()
                data = batch([source], [target], [mapping])
                if location == "calendar":
                    data["calendars"][0]["id"] = "personal@example.invalid"
                elif location == "namespace":
                    data["namespace"] = "a" * 32
                else:
                    data["state"]["mappings"][0]["destination"]["calendar_id"] = "personal@example.invalid"
                self.assert_blocked(data)

    def test_wrong_window_is_not_substituted_with_midnight(self):
        data = batch([event()])
        data["calendars"][0]["listing"]["time_min"] = "2026-10-06T00:00:00Z"
        self.assert_blocked(data, "window_must_be_run_start_plus_90_days")

    def test_absent_and_uncertain_state_never_produce_mutations(self):
        for status in ("absent", "uncertain"):
            self.assert_blocked(batch([event()], status=status), f"state_{status}_requires_reconstruction")

    def test_empty_unverified_state_still_requires_reconstruction(self):
        self.assert_blocked(batch(status="absent"), "state_absent_requires_reconstruction")

    def test_absent_state_yields_only_verified_canonical_recovery_candidate(self):
        source = event()
        target = mirror(source)
        result = self.assert_blocked(batch([source], [target], status="absent"))
        self.assertEqual(result["reconstruction_candidates"], [registry(source, target)])

    def test_uncertain_existing_mapping_does_not_emit_update_or_delete(self):
        source, target, mapping = paired()
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        self.assert_blocked(batch([source], [target], [mapping], status="uncertain"))
        source["fields"]["transparency"] = "transparent"
        self.assert_blocked(batch([source], [target], [mapping], status="uncertain"))

    def test_unregistered_mirror_never_auto_adopts_even_in_verified_state(self):
        source = event()
        result = p.plan(batch([source], [mirror(source)]))
        self.assert_reason(result, "unregistered_mirror_requires_reconstruction")
        self.assertEqual(writes(result), [])

    def test_reconstruction_rejects_time_or_non_time_modified_unknown_mirror(self):
        source = event()
        for kind in ("time", "non_time"):
            target = mirror(source)
            if kind == "time":
                target["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
            else:
                target["fields"]["summary"] = "Manual"
            result = self.assert_blocked(batch([source], [target], status="absent"))
            self.assertEqual(result["reconstruction_candidates"], [])

    def test_source_not_in_listing_does_not_imply_delete(self):
        source, target, mapping = paired()
        result = p.plan(batch([], [target], [mapping]))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "source_missing_without_terminal_verification")

    def test_error_or_ambiguous_404_never_means_deleted(self):
        source, target, mapping = paired()
        for outcome in ("error", "not_found"):
            result = p.plan(batch([], [target], [mapping], responses=[terminal(A, source["id"], outcome), found(B, target)]))
            self.assertEqual(writes(result), [])
            self.assert_reason(result, "source_missing_without_terminal_verification")

    def test_known_cancelled_and_deleted_future_mirrors_can_delete(self):
        source, target, mapping = paired()
        for outcome in ("cancelled", "deleted"):
            data = batch([], [target], [mapping], responses=[terminal(A, source["id"], outcome), found(B, target)])
            action = writes(p.plan(data))[0]
            self.assertEqual(action["op"], "delete")
            self.assertEqual(action["expected"]["source"]["outcome"], outcome)
            self.assertEqual(action["expected"]["destination"]["event"], target)

    def test_terminal_result_needs_known_id_proof(self):
        source, target, mapping = paired()
        data = batch([], [target], [mapping], responses=[terminal(A, source["id"], "deleted"), found(B, target)])
        data["details"]["responses"][0]["evidence"]["verified_known_id"] = False
        self.assert_blocked(data, "terminal_detail_requires_proof")

    def test_verified_free_or_declined_source_can_delete(self):
        for kind in ("free", "self_declined", "cancelled"):
            source, target, mapping = paired()
            if kind == "free":
                source["fields"]["transparency"] = "transparent"
            elif kind == "self_declined":
                with_response(source, "declined")
            else:
                source["status"] = "cancelled"
            action = writes(p.plan(batch([source], [target], [mapping])))[0]
            self.assertEqual(action["op"], "delete")
            self.assertEqual(action["reason"], "known_source_" + kind)

    def test_moved_beyond_window_with_known_id_verification_deletes_current_mirror(self):
        source, target, mapping = paired(recurring())
        source["start"]["dateTime"] = "2027-06-01T18:00:00+09:00"
        source["end"]["dateTime"] = "2027-06-01T19:00:00+09:00"
        data = batch([], [target], [mapping], responses=[found(A, source), found(B, target)])
        action = writes(p.plan(data))[0]
        self.assertEqual(action["op"], "delete")
        self.assertEqual(action["reason"], "known_source_outside_future_window")

    def test_ended_past_mirrors_never_bulk_deleted_even_after_cancellation(self):
        source, target, mapping = paired(event(begin="2026-10-01T18:00:00+09:00", end="2026-10-01T19:00:00+09:00"))
        data = batch([], [], [mapping], responses=[terminal(A, source["id"], "cancelled"), found(B, target)])
        result = p.plan(data)
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "ended_past_mirror_retained")

    def test_source_moved_to_past_releases_future_mirror_after_known_id_verification(self):
        source, target, mapping = paired()
        source["start"]["dateTime"] = "2026-10-01T18:00:00+09:00"
        source["end"]["dateTime"] = "2026-10-01T19:00:00+09:00"
        data = batch([], [target], [mapping], responses=[found(A, source), found(B, target)])
        result = p.plan(data)
        self.assertEqual(writes(result)[0]["op"], "delete")
        self.assert_reason(result, "known_source_moved_before_window")

    def test_naturally_ended_source_does_not_bulk_clean_mirrors(self):
        source, target, mapping = paired(event(begin="2026-10-01T18:00:00+09:00", end="2026-10-01T19:00:00+09:00"))
        target["start"]["dateTime"] = "2026-10-07T18:00:00+09:00"
        target["end"]["dateTime"] = "2026-10-07T19:00:00+09:00"
        result = p.plan(batch([], [target], [mapping], responses=[found(A, source), found(B, target)]))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "ended_past_source_retained_no_bulk_cleanup")

    def test_missing_registered_destination_is_not_blindly_recreated(self):
        source, target, mapping = paired()
        for outcome in ("not_found", "deleted", "cancelled", "error"):
            result = p.plan(batch([source], [], [mapping], responses=[found(A, source), terminal(B, target["id"], outcome)]))
            self.assertEqual(writes(result), [])
            self.assert_reason(result, "registered_destination_missing_requires_reconciliation")

    def test_marker_at_different_id_requires_reconciliation(self):
        source, target, mapping = paired()
        other = deepcopy(target)
        other["id"] = "unknown-retry-id"
        data = batch([source], [other], [mapping], responses=[found(A, source), terminal(B, target["id"], "deleted")])
        result = p.plan(data)
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "marker_at_unregistered_destination_id")

    def test_manual_edit_protected_even_when_source_deleted(self):
        source, target, mapping = paired()
        target["fields"]["location"] = "Manual"
        data = batch([], [target], [mapping], responses=[terminal(A, source["id"], "deleted"), found(B, target)])
        result = p.plan(data)
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "destination_manual_non_time_edit")

    def test_default_opaque_tentative_needs_action_are_busy(self):
        for transparency in (None, "opaque"):
            for response in ("none", "accepted", "tentative", "needsAction"):
                for status in ("confirmed", "tentative"):
                    source = event()
                    source["fields"]["transparency"] = transparency
                    source["status"] = status
                    if response != "none":
                        with_response(source, response)
                    self.assertEqual(p.plan(batch([source]))["counts"]["create"], 1)

    def test_exclusions_do_not_create(self):
        for kind in ("cancelled", "transparent", "declined"):
            source = event()
            if kind == "cancelled":
                source["status"] = "cancelled"
            elif kind == "transparent":
                source["fields"]["transparency"] = "transparent"
            else:
                with_response(source, "declined")
            self.assertEqual(writes(p.plan(batch([source]))), [])

    def test_ongoing_included_and_window_edges_are_half_open(self):
        cases = [("2026-10-06T11:00:00Z", "2026-10-06T13:00:00Z", 1),
                 ("2026-10-06T11:00:00Z", RUN, 0),
                 ("2027-01-04T12:00:00Z", "2027-01-04T13:00:00Z", 0),
                 ("2027-01-04T11:59:59Z", "2027-01-04T13:00:00Z", 1)]
        for begin, end, count in cases:
            with self.subTest(begin=begin):
                self.assertEqual(p.plan(batch([event(begin=begin, end=end)]))["counts"]["create"], count)

    def test_idempotent_second_run_after_verified_create(self):
        source = event()
        first = p.plan(batch([source]))
        target = mirror(source)
        self.assertEqual(first["counts"]["create"], 1)
        second = p.plan(batch([source], [target], [registry(source, target)]))
        self.assertEqual(second["counts"]["noop"], 1)
        self.assertEqual(writes(second), [])

    def test_idempotent_second_run_after_verified_update(self):
        source, target, mapping = paired()
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        first = p.plan(batch([source], [target], [mapping]))
        self.assertEqual(first["counts"]["update"], 1)
        target.update({k: deepcopy(v) for k, v in writes(first)[0]["desired"].items()})
        target["etag"] = '"updated"'
        second = p.plan(batch([source], [target], [registry(source, target)]))
        self.assertEqual(writes(second), [])
        self.assertEqual(second["counts"]["noop"], 1)

    def test_input_is_not_mutated_and_result_is_deterministic_under_ordering(self):
        a, b = event("a"), event("b")
        ma, mb = mirror(b, B, "ma"), mirror(a, A, "mb")
        data = batch([a, ma, event("extra")], [b, mb], [registry(a, mb), registry(b, ma, B)])
        before = deepcopy(data)
        expected = p.plan(data)
        self.assertEqual(data, before)
        for seed in range(20):
            shuffled = deepcopy(data)
            rng = random.Random(seed)
            rng.shuffle(shuffled["calendars"])
            rng.shuffle(shuffled["state"]["mappings"])
            rng.shuffle(shuffled["details"]["requested"])
            rng.shuffle(shuffled["details"]["responses"])
            for calendar in shuffled["calendars"]:
                rng.shuffle(calendar["listing"]["events"])
                rng.shuffle(calendar["marker_searches"])
            self.assertEqual(p.plan(shuffled), expected)

    def test_inconsistent_read_observations_fail_closed(self):
        source, target, mapping = paired()
        data = batch([source], [target], [mapping])
        data["details"]["responses"][0]["event"]["etag"] = '"new"'
        self.assert_blocked(data, "inconsistent_event_snapshots")

    def test_baseline_corruption_duplicate_mapping_and_loop_rejected(self):
        source, target, mapping = paired()
        data = batch([source], [target], [mapping])
        data["state"]["mappings"][0]["marker"] = "invalid"
        self.assert_blocked(data, "registry_marker_hash_mismatch")
        data = batch([source], [target], [mapping])
        data["state"]["mappings"].append(deepcopy(mapping))
        self.assert_blocked(data, "duplicate_registry_identity")
        data = batch([source], [target], [mapping])
        data["state"]["mappings"][0]["verified_destination"]["fields"]["summary"] = "manual"
        self.assert_blocked(data, "registry_baseline_not_canonical_mirror")

    def test_invalid_time_forms_fail_closed(self):
        for bad in ("2026-10-07T18:00:00", "2026-02-30T18:00:00Z", "2026-10-07", "not-time",
                    "2026-10-07T18:00:00+00:99", "2026-10-07T18:00:00-00:00"):
            source = event()
            source["start"]["dateTime"] = bad
            self.assert_blocked(batch([source]))
        source = all_day()
        source["all_day_bounds"]["start"] = "2026-10-06T00:00:00+09:00"
        self.assert_blocked(batch([source]), "all_day_boundary_mismatch")

    def test_all_day_bounds_and_self_response_cannot_be_guessed(self):
        source = all_day()
        source["all_day_bounds"] = None
        self.assert_blocked(batch([source]), "all_day_requires_resolved_bounds")
        source = event()
        source["self_response"] = "declined"
        self.assert_blocked(batch([source]), "self_response_not_verified_from_attendees")

    def test_raw_object_original_start_and_unexpanded_recurrence_rejected(self):
        source = event()
        source["original_start_time"] = {"dateTime": "2026-10-07T18:00:00+09:00"}
        self.assert_blocked(batch([source]), "original_start_must_be_raw_string_or_null")
        source = event()
        source["fields"]["other"]["recurrence"] = ["RRULE:FREQ=DAILY"]
        self.assert_blocked(batch([source]), "unexpanded_recurrence")

    def test_deleted_response_contradicting_listing_blocks_batch(self):
        source, target, mapping = paired()
        data = batch([source], [target], [mapping], responses=[terminal(A, source["id"], "deleted"), found(B, target)])
        self.assert_blocked(data, "terminal_detail_contradicts_observation")

    def test_unknown_top_level_and_structural_mutations_fail_closed(self):
        data = batch([event()])
        data["personal_calendar"] = "forbidden"
        self.assert_blocked(data)
        for value in (None, [], "string", {"schema_version": 1}, 42):
            self.assert_blocked(value)
        for key in list(batch()):
            data = batch([event()])
            del data[key]
            self.assert_blocked(data)

    def test_cli_stdin_and_output_file_without_network(self):
        data = batch([event()])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan.json"
            with patch("sys.stdin", io.StringIO(json.dumps(data))):
                code = p.main(["-", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), p.plan(data))

    def test_cli_rejects_duplicate_keys_nan_and_malformed_json(self):
        for text in ('{"schema_version":1,"schema_version":1}', '{"n": NaN}', 'bad-json'):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(text)), patch("sys.stdout", output):
                self.assertEqual(p.main([]), 2)
            self.assertEqual(writes(json.loads(output.getvalue())), [])

    def test_cli_exit_status_review_required(self):
        source = event()
        data = batch([source], [mirror(source)])
        with patch("sys.stdin", io.StringIO(json.dumps(data))), patch("sys.stdout", io.StringIO()):
            self.assertEqual(p.main([]), 1)


class ConnectorCompatibilityTests(unittest.TestCase):
    assert_reason = PlanningTests.assert_reason
    assert_blocked = PlanningTests.assert_blocked
    def test_projection_no_etag_all_operations_have_snapshot_evidence(self):
        source, target, mapping = paired()
        unchanged = p.plan(projection(batch([source], [target], [mapping])))
        self.assertEqual(unchanged["counts"]["noop"], 1)
        self.assertFalse(unchanged["concurrency"]["provider_cas_available"])
        created = writes(p.plan(projection(batch([event()]))))[0]
        self.assertEqual(created["op"], "create")
        self.assertIsNone(created["expected"]["source"]["event"]["etag"])
        self.assertEqual(len(created["expected"]["fingerprints"]["source"]), 64)
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        update = writes(p.plan(projection(batch([source], [target], [mapping]))))[0]
        self.assertEqual(update["op"], "update")
        source["fields"]["transparency"] = "transparent"
        delete = writes(p.plan(projection(batch([source], [target], [mapping]))))[0]
        self.assertEqual(delete["op"], "delete")
        for action in (created, update, delete):
            check = p.revalidate_action(action, fresh_for(action))
            self.assertTrue(check["allowed"], check)
            self.assertIsNone(check["destination_if_match_etag"])
            self.assertFalse(check["concurrency"]["atomic_cross_event_guarantee"])

    def test_explicit_capability_mode_cannot_fabricate_etag_or_cas(self):
        data = projection(batch([event()]))
        data["calendars"][0]["listing"]["events"][0]["etag"] = '"fake"'
        self.assert_blocked(data, "etag_does_not_match_declared_capability")
        data = projection(batch([event()]))
        data["connector_capabilities"]["conditional_writes"] = True
        self.assert_blocked(data, "contradictory_connector_capabilities")
        data = batch([event()])
        data["calendars"][0]["listing"]["events"][0]["etag"] = None
        self.assert_blocked(data, "etag_does_not_match_declared_capability")

    def test_no_capability_mode_is_not_silently_assumed(self):
        data = batch([event()])
        del data["connector_capabilities"]
        self.assert_blocked(data)

    def test_projection_unknown_fields_are_explicit_and_cannot_be_assumed_absent(self):
        for field, value in (("conferenceData", None),):
            data = projection(batch([event()]))
            data["calendars"][0]["listing"]["events"][0]["fields"][field] = value
            self.assert_blocked(data, "unobservable_field_must_not_be_invented")
        data = projection(batch([event()]))
        data["calendars"][0]["listing"]["events"][0]["fields"]["other"] = {}
        self.assert_blocked(data, "projection_other_requires_observed_subset")

    def test_exposed_additional_fields_remain_protected_with_unknown_provider_remainder(self):
        source, target, mapping = paired()
        target["fields"]["other"]["color_id"] = "changed"
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        result = p.plan(projection(batch([source], [target], [mapping])))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "destination_manual_non_time_edit")

    def test_exposed_event_type_is_checked_instead_of_discarded(self):
        source, target, mapping = paired()
        data = projection(batch([source], [target], [mapping]))
        for collection in (data["calendars"][1]["listing"]["events"],
                           [data["details"]["responses"][1]["event"]]):
            collection[0]["fields"]["eventType"] = "focusTime"
        result = p.plan(data)
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "destination_manual_non_time_edit")

    def test_projection_observable_fields_must_still_be_complete(self):
        for field in ("hangout_link", "recurrence", "reminders", "visibility", "attendees", "location"):
            data = projection(batch([event()]))
            del data["calendars"][0]["listing"]["events"][0]["fields"][field]
            self.assert_blocked(data, "partial_or_unknown_writable_fields")

    def test_projection_hangout_link_manual_change_blocks_update_and_delete(self):
        source, target, mapping = paired()
        target["fields"]["hangout_link"] = "https://meet.example.invalid/manual"
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        for value in (None, "transparent"):
            source["fields"]["transparency"] = value
            result = p.plan(projection(batch([source], [target], [mapping])))
            self.assertEqual(writes(result), [])
            self.assert_reason(result, "destination_manual_non_time_edit")

    def test_no_all_time_expansion_needed_for_registered_noop_update_delete(self):
        source, target, mapping = paired()
        for mode in ("noop", "update", "delete"):
            if mode == "update":
                source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
            if mode == "delete":
                source["fields"]["transparency"] = "transparent"
            data = projection(batch([source], [target], [mapping]))
            for calendar in data["calendars"]:
                calendar["marker_searches"] = []
            result = p.plan(data)
            self.assertEqual(result["counts"][mode], 1, result)
            self.assertEqual(result["counts"]["conflict"], 0)

    def test_out_of_window_registered_destination_is_still_read_by_id(self):
        source, target, mapping = paired()
        target["start"]["dateTime"] = "2027-06-01T18:00:00+09:00"
        target["end"]["dateTime"] = "2027-06-01T19:00:00+09:00"
        data = projection(batch([source], [], [mapping], responses=[found(A, source), found(B, target)]))
        data["calendars"][1]["marker_searches"] = []
        self.assertEqual(p.plan(data)["counts"]["update"], 1)

    def test_unverified_marker_lookup_stops_create_and_unknown_adoption_only(self):
        source = event()
        for targets in ([], [mirror(source)]):
            data = projection(batch([source], targets))
            data["calendars"][1]["marker_searches"] = []
            result = p.plan(data)
            self.assertEqual(writes(result), [])
            self.assertEqual(result["reconstruction_candidates"], [])
            self.assert_reason(result, "marker_search_not_certified_for_create_or_adoption")

    def test_marker_search_out_of_window_duplicate_is_detected_by_whole_description(self):
        source = event()
        target = mirror(source)
        target["start"]["dateTime"] = "2027-06-01T18:00:00+09:00"
        target["end"]["dateTime"] = "2027-06-01T19:00:00+09:00"
        data = batch([source])
        data["calendars"][1]["marker_searches"][0]["events"] = [target]
        result = p.plan(projection(data))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "unregistered_mirror_requires_reconstruction")
        data["calendars"][1]["marker_searches"][0]["events"][0]["fields"]["description"] += " manual"
        result = p.plan(projection(data))
        self.assertEqual(writes(result), [])
        self.assert_reason(result, "malformed_description_contains_matching_marker")

    def test_registered_id_inventory_completeness_cannot_be_faked_by_omission(self):
        source, target, mapping = paired()
        data = projection(batch([source], [target], [mapping]))
        data["state"]["mappings_complete"] = False
        self.assert_blocked(data, "registry_not_complete")

    def test_snapshot_fingerprint_ignores_object_key_order_but_not_values(self):
        first = {"a": 1, "b": {"c": 2, "d": 3}}
        second = {"b": {"d": 3, "c": 2}, "a": 1}
        self.assertEqual(p.snapshot_fingerprint(first), p.snapshot_fingerprint(second))
        second["b"]["c"] = 4
        self.assertNotEqual(p.snapshot_fingerprint(first), p.snapshot_fingerprint(second))

    def test_immediate_complete_reread_certificates_are_all_mandatory(self):
        action = writes(p.plan(projection(batch([event()]))))[0]
        for key in p.REREAD_CERTIFICATE_KEYS:
            fresh = fresh_for(action)
            fresh["reread_certificate"][key] = False
            result = p.revalidate_action(action, fresh)
            self.assertFalse(result["allowed"])
            self.assertEqual(result["reason"], "immediate_complete_rereads_required")

    def test_prewrite_guard_rejects_changed_source_destination_inventory_and_state(self):
        source, target, mapping = paired()
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        action = writes(p.plan(projection(batch([source], [target], [mapping]))))[0]
        mutations = [lambda f: f["source"]["event"]["fields"].update(summary="changed"),
                     lambda f: f["destination"]["event"]["fields"].update(location="manual"),
                     lambda f: f["destination"]["event"]["fields"].update(description="erased"),
                     lambda f: f["marker_inventory"]["matching_events"].append(deepcopy(target)),
                     lambda f: f.update(state_generation="changed"),
                     lambda f: f.update(state_status="uncertain")]
        for mutation in mutations:
            fresh = fresh_for(action)
            mutation(fresh)
            self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_prewrite_create_rejects_marker_appearing_after_plan(self):
        source = event()
        action = writes(p.plan(projection(batch([source]))))[0]
        fresh = fresh_for(action)
        fresh["marker_inventory"]["matching_events"].append(mirror(source))
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_guard_provides_real_etag_only_in_conditional_write_mode(self):
        source, target, mapping = paired()
        source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        action = writes(p.plan(batch([source], [target], [mapping])))[0]
        checked = p.revalidate_action(action, fresh_for(action))
        self.assertTrue(checked["allowed"])
        self.assertEqual(checked["destination_if_match_etag"], target["etag"])

    def test_revalidate_rejects_noop_and_tampered_action(self):
        source, target, mapping = paired()
        action = p.plan(projection(batch([source], [target], [mapping])))["actions"][0]
        self.assertFalse(p.revalidate_action(action, {})["allowed"])
        action = writes(p.plan(projection(batch([event()]))))[0]
        fresh = fresh_for(action)
        action["desired"]["fields"]["summary"] = "tampered"
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_legacy_bootstrap_uses_current_readbacks_without_inventing_history(self):
        source, target, mapping = paired(recurring())
        source["original_start_time"] = "2026-10-07T09:00:00Z"
        data = legacy_bootstrap(batch([source], [target], [mapping]))
        for calendar in data["calendars"]:
            calendar["marker_searches"] = []
        self.assertNotIn("verified_source", data["state"]["mappings"][0])
        result = p.plan(data)
        self.assertEqual(result["status"], "bootstrap_ready", result)
        self.assertEqual(writes(result), [])
        self.assertEqual(result["counts"]["noop"], 1)
        restored = result["bootstrap_mappings"][0]
        self.assertEqual(restored["marker"], mapping["marker"])
        self.assertEqual(restored["source"], mapping["source"])
        self.assertEqual(restored["destination"], mapping["destination"])
        self.assertIsNone(restored["verified_destination"]["etag"])
        data["state"].update(status="verified", mappings=result["bootstrap_mappings"], generation="after-bootstrap")
        second = p.plan(data)
        self.assertEqual(second["status"], "ready")
        self.assertEqual(second["counts"]["noop"], 1)

    def test_legacy_bootstrap_blocks_time_status_response_and_protected_changes(self):
        for kind in ("time", "status", "self_response", "source_time", "summary", "hangout_link"):
            source, target, mapping = paired()
            if kind == "time":
                target["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
            elif kind == "source_time":
                source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
            elif kind == "status":
                source["status"] = "tentative"
            elif kind == "self_response":
                with_response(source, "needsAction")
            else:
                target["fields"][kind] = "manual"
            result = self.assert_blocked(legacy_bootstrap(batch([source], [target], [mapping])))
            self.assertEqual(result["bootstrap_mappings"], [])

    def test_legacy_bootstrap_rejects_missing_proof_and_incomplete_registry(self):
        source, target, mapping = paired()
        data = legacy_bootstrap(batch([source], [target], [mapping]))
        data["state"]["mappings"][0]["legacy"]["verified"] = False
        self.assert_blocked(data, "legacy_verification_evidence_required")
        data = legacy_bootstrap(batch([source], [target], [mapping]))
        data["state"]["mappings_complete"] = False
        self.assert_blocked(data, "registry_not_complete")

    def test_legacy_cannot_be_marked_verified_without_bootstrap(self):
        source, target, mapping = paired()
        data = legacy_bootstrap(batch([source], [target], [mapping]))
        data["state"]["status"] = "verified"
        self.assert_blocked(data, "legacy_mappings_require_explicit_bootstrap_state")

    def test_bootstrap_defers_new_native_events_without_requiring_create_lookups(self):
        source, target, mapping = paired()
        data = legacy_bootstrap(batch([source, event("new-native")], [target], [mapping]))
        for calendar in data["calendars"]:
            calendar["marker_searches"] = []
        result = p.plan(data)
        self.assertEqual(result["status"], "bootstrap_ready", result)
        self.assertEqual(writes(result), [])
        self.assertEqual(len(result["bootstrap_mappings"]), 1)
        self.assert_reason(result, "unregistered_native_deferred_during_bootstrap")

    def test_bootstrap_does_not_ignore_unregistered_owned_mirrors(self):
        source, target, mapping = paired()
        other = event("other-source")
        data = legacy_bootstrap(batch([source, other], [target, mirror(other, event_id="orphan")], [mapping]))
        result = self.assert_blocked(data)
        self.assertFalse(result["bootstrap_complete"])
        self.assert_reason(result, "orphan_owned_marker_requires_reconstruction")

    def test_legacy_missing_destination_is_not_recreated(self):
        source, target, mapping = paired()
        result = self.assert_blocked(legacy_bootstrap(batch([source], [], [mapping])))
        self.assertEqual(result["bootstrap_mappings"], [])

    def test_synthetic_279_legacy_rows_bootstrap_without_etags_or_all_time_native_scan(self):
        events, mappings = {A: [], B: []}, []
        for index in range(279):
            calendar = A if index % 2 else B
            other = B if calendar == A else A
            stamp = (p.instant("2026-10-07T09:00:00Z") + timedelta(hours=index)).isoformat()
            source = event("legacy-source-" + str(index), stamp, (p.instant(stamp) + timedelta(minutes=30)).isoformat())
            if index < 277:
                source.update(original_start_time=stamp, recurring_event_id="legacy-series")
            target = mirror(source, calendar, "legacy-destination-" + str(index))
            mappings.append(registry(source, target, calendar))
            events[calendar].append(source)
            events[other].append(target)
        data = legacy_bootstrap(batch(events[A], events[B], mappings))
        for calendar in data["calendars"]:
            calendar["marker_searches"] = []
        result = p.plan(data)
        self.assertEqual(result["status"], "bootstrap_ready", result)
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 279, "conflict": 0})
        self.assertEqual(len(result["bootstrap_mappings"]), 279)

    def test_projection_cli_revalidation(self):
        action = writes(p.plan(projection(batch([event()]))))[0]
        payload = {"action": action, "fresh": fresh_for(action)}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch("sys.stdout", output):
            self.assertEqual(p.main(["--revalidate"]), 0)
        self.assertTrue(json.loads(output.getvalue())["allowed"])




def managed(data):
    result = deepcopy(data)
    ledger = {"version": 2, "config_fingerprint": result["state"]["config_fingerprint"], "status": "verified", "complete": True, "durable": True,
              "generation": "synthetic-ledger-1",
              "provenance": {"kind": "verified_history_and_state", "history_complete": True,
                             "state_verified": True, "evidence": "synthetic-complete-history-and-bootstrap-audit"},
              "issued": [], "operations": []}
    for index, item in enumerate(result["state"]["mappings"]):
        ledger["issued"].append({"marker": item["marker"], "destination_calendar_id": item["destination"]["calendar_id"],
                                  "destination_ids": [item["destination"]["event_id"]], "disposition": "mapped"})
        ledger["operations"].append({"operation_id": f"synthetic-migrated-{index}", "marker": item["marker"],
                                     "status": "committed", "action_id": None})
    result["managed_context"] = {"ledger": ledger, "single_writer": {"externally_enforced": True, "token": "synthetic-fence-1"}}
    for calendar in result["calendars"]:
        for search in calendar["marker_searches"]:
            search["scope"] = p.MANAGED_SCOPE
            search["queries"] = [{"kind": kind, "q": q, "time_min": None, "time_max": None,
                                  "complete": True, "next_page_token": None}
                                 for kind, q in (("namespace", result["config"]["namespace"]), ("key", search["marker"][-65:-1]))]
    return result


def claimed_fresh(action):
    fresh = fresh_for(action)
    context = deepcopy(action["expected"]["managed_context"])
    ledger = context["ledger"]
    ledger["generation"] = "synthetic-prepared-2"
    oid = "synthetic-operation-new"
    operation = {"operation_id": oid, "marker": action["marker"], "status": "prepared", "action_id": action["id"]}
    if "recovery" in action["expected"]:
        oid += "-recovery-" + str(len(ledger["operations"]))
        operation.update(operation_id=oid, recovery_id=action["expected"]["recovery"]["recovery_id"])
        ledger["generation"] += "-recovery-" + str(len(ledger["operations"]))
        next(r for r in ledger["recoveries"] if r["recovery_id"] == operation["recovery_id"])["consumed_by_operation_id"] = oid
    else:
        ledger["issued"].append({"marker": action["marker"], "destination_calendar_id": action["destination"]["calendar_id"],
                                  "destination_ids": [], "disposition": "unresolved"})
    ledger["operations"].append(operation)
    fresh["creation_claim"] = {"operation_id": oid, "action_id": action["id"],
        "prepared_generation": ledger["generation"], "prepared_ledger_fingerprint": p.digest(ledger),
        "durable_intent_persisted": True, "atomic_once_only_claim": True, "from_status": "prepared", "to_status": "attempt_started"}
    if context["single_writer"].get("mode") == "serialized_runner":
        del fresh["creation_claim"]["atomic_once_only_claim"]
        fresh["creation_claim"].update(mode="serialized_runner", intent_readback_verified=True,
            attempt_started_readback_verified=True, same_uninterrupted_admitted_execution=True,
            calendar_call_not_yet_attempted=True, resume_or_retry=False)
    ledger["generation"] = "synthetic-attempt-started-3"
    if "recovery" in action["expected"]:
        ledger["generation"] += "-recovery-" + str(len(ledger["operations"]))
    ledger["operations"][-1]["status"] = "attempt_started"
    fresh["managed_context"] = context
    fresh["reread_certificate"].update(all_ledger_destination_ids=True, ledger_and_single_writer_reread=True)
    return fresh


def serialized(data):
    result = deepcopy(data)
    result["managed_context"]["single_writer"] = {"mode": "serialized_runner", "token": "synthetic-root-admission-1",
        "root_coordinated": True, "cutover_paused_and_drained": True, "only_one_execution_admitted": True}
    return result


class ManagedCoverageTests(unittest.TestCase):
    def source_data(self):
        return managed(projection(batch([event()])))

    def create_action(self, data=None):
        result = p.plan(data or self.source_data())
        self.assertEqual(result["counts"]["create"], 1, result)
        return next(a for a in result["actions"] if a["op"] == "create")

    def no_create(self, data, reason=None):
        result = p.plan(data)
        self.assertEqual(result["counts"]["create"], 0, result)
        if reason:
            self.assertIn(reason, [a["reason"] for a in result["actions"]], result)
        return result

    def test_never_issued_marker_creates_with_truthful_managed_coverage(self):
        action = self.create_action()
        self.assertEqual(action["expected"]["marker_inventory"]["marker_search"]["scope"], p.MANAGED_SCOPE)
        self.assertIn("not universal", action["notes"][0])
        self.assertTrue(action["write_contract"]["durable_intent_then_atomic_once_only_attempt_claim_required"])

    def test_bidirectional_new_creates(self):
        result = p.plan(managed(projection(batch([event("a")], [event("b")]))))
        self.assertEqual(result["counts"]["create"], 2)

    def test_missing_ledger_is_never_assumed_empty(self):
        data = self.source_data()
        del data["managed_context"]
        self.no_create(data, "managed_creation_requires_durable_ledger_and_single_writer")

    def test_uncertain_absent_bootstrap_state_prevents_managed_create(self):
        for status in ("absent", "uncertain", "bootstrap"):
            with self.subTest(status=status):
                data = self.source_data()
                data["state"]["status"] = status
                self.no_create(data)

    def test_missing_or_uncertain_ledger_certificates(self):
        for key, value in (("complete", False), ("durable", False), ("status", "uncertain"), ("generation", "")):
            with self.subTest(key=key):
                data = self.source_data()
                data["managed_context"]["ledger"][key] = value
                self.no_create(data, "complete_verified_durable_ledger_required")

    def test_migration_requires_complete_history_and_verified_state_evidence(self):
        for key, value in (("history_complete", False), ("state_verified", False), ("evidence", ""), ("kind", "guess")):
            with self.subTest(key=key):
                data = self.source_data()
                data["managed_context"]["ledger"]["provenance"][key] = value
                self.no_create(data, "ledger_migration_evidence_required")

    def test_external_single_writer_required(self):
        data = self.source_data()
        data["managed_context"]["single_writer"]["externally_enforced"] = False
        self.no_create(data, "external_single_writer_required")

    def test_previously_issued_marker_blocks_even_after_definite_no_write(self):
        data = self.source_data()
        marker = data["calendars"][1]["marker_searches"][0]["marker"]
        ledger = data["managed_context"]["ledger"]
        ledger["issued"] = [{"marker": marker, "destination_calendar_id": B, "destination_ids": [], "disposition": "retired"}]
        ledger["operations"] = [{"operation_id": "prior", "marker": marker, "status": "verified_no_write", "action_id": None}]
        self.no_create(data, "previously_issued_marker_requires_verified_mapping")

    def test_unresolved_operation_stops_managed_creates_conservatively(self):
        for status in ("prepared", "attempt_started", "uncertain"):
            with self.subTest(status=status):
                data = self.source_data()
                marker = p.marker_for(CONFIG, A, "other-native", None, B)
                ledger = data["managed_context"]["ledger"]
                ledger["issued"] = [{"marker": marker, "destination_calendar_id": B, "destination_ids": [], "disposition": "unresolved"}]
                ledger["operations"] = [{"operation_id": "prior", "marker": marker, "status": status, "action_id": "prior-action"}]
                self.no_create(data, "managed_ledger_has_unresolved_operations")

    def known_and_new(self):
        source, target, mapping = paired(event("known-source"))
        return managed(projection(batch([source, event("new-source")], [target], [mapping])))

    def test_known_mapped_mirror_does_not_loop_or_block_new_source(self):
        result = p.plan(self.known_and_new())
        self.assertEqual(result["counts"], {"create": 1, "update": 0, "delete": 0, "noop": 1, "conflict": 0})

    def test_omitted_known_marker_blocks_new_create(self):
        data = self.known_and_new()
        data["managed_context"]["ledger"].update(issued=[], operations=[])
        self.no_create(data, "ledger_omits_registered_issued_marker")

    def test_issued_without_mapping_blocks(self):
        data = self.known_and_new()
        data["state"]["mappings"] = []
        self.no_create(data, "issued_marker_without_verified_mapped_destination")

    def test_out_of_window_tracked_destination_read_in_expected_snapshot(self):
        data = self.known_and_new()
        data["calendars"][1]["listing"]["events"] = []
        action = self.create_action(data)
        observations = action["expected"]["marker_inventory"]["tracked_destination_observations"]
        self.assertEqual([r["event_id"] for r in observations], ["mirror-1"])

    def test_unread_ledger_destination_blocks_even_if_not_mapped(self):
        data = self.known_and_new()
        data["state"]["mappings"] = []
        data["details"]["responses"] = []
        data["details"]["requested"] = []
        self.no_create(data, "ledger_destination_ids_require_explicit_detail_reads")

    def test_damaged_out_of_window_tracked_mirror_blocks_new_create(self):
        for field, value in (("description", "manually erased"), ("summary", "manual title"), ("location", "manual room")):
            with self.subTest(field=field):
                data = self.known_and_new()
                data["calendars"][1]["listing"]["events"] = []
                next(r for r in data["details"]["responses"] if r["event_id"] == "mirror-1")["event"]["fields"][field] = value
                self.no_create(data, "tracked_destination_missing_or_manually_changed")

    def test_retired_destination_requires_explicit_known_id_terminal_read(self):
        data = self.known_and_new()
        data["state"]["mappings"] = []
        data["managed_context"]["ledger"]["issued"][0]["disposition"] = "retired"
        data["calendars"][0]["listing"]["events"] = [data["calendars"][0]["listing"]["events"][-1]]
        data["calendars"][1]["listing"]["events"] = []
        data["details"]["responses"] = [terminal(B, "mirror-1", "deleted")]
        data["details"]["requested"] = [p.ref(B, "mirror-1")]
        self.create_action(data)
        data["details"]["responses"] = [terminal(B, "mirror-1", "not_found")]
        self.no_create(data, "retired_destination_requires_terminal_known_id_verification")

    def test_unread_bounded_missing_or_wrong_queries_block(self):
        cases = [("time_min", RUN), ("time_max", RUN), ("complete", False), ("next_page_token", "more"), ("q", "wrong")]
        for key, value in cases:
            with self.subTest(key=key):
                data = self.source_data()
                data["calendars"][1]["marker_searches"][0]["queries"][0][key] = value
                self.no_create(data)
        data = self.source_data()
        data["calendars"][1]["marker_searches"][0]["queries"].pop()
        self.no_create(data, "namespace_and_key_queries_required")

    def test_incomplete_window_blocks_despite_exhausted_queries(self):
        data = self.source_data()
        data["calendars"][0]["listing"]["next_page_token"] = "more"
        self.no_create(data, "incomplete_calendar_pages")

    def test_same_marker_and_extra_marker_text_never_adopted(self):
        source = event()
        for text in (mirror(source)["fields"]["description"], mirror(source)["fields"]["description"] + " text"):
            with self.subTest(text=text[-6:]):
                target = mirror(source)
                target["fields"]["description"] = text
                self.no_create(managed(projection(batch([source], [target]))))

    def test_unrelated_untracked_owned_marker_blocks_managed_create(self):
        data = managed(projection(batch([event()], [mirror(event("other"))])))
        self.no_create(data, "managed_inventory_contains_untracked_owned_marker")

    def test_cancelled_unknown_owned_marker_still_contradicts_never_issued_history(self):
        source = event()
        target = mirror(source)
        target["status"] = "cancelled"
        self.no_create(managed(projection(batch([source], [target]))), "managed_inventory_contains_untracked_owned_marker")

    def test_cancelled_malformed_marker_is_not_absence_evidence(self):
        source = event()
        target = mirror(source)
        target["status"] = "cancelled"
        target["fields"]["description"] += " manual"
        self.no_create(managed(projection(batch([source], [target]))), "managed_inventory_contains_malformed_marker")

    def test_guard_requires_durable_intent_and_once_only_claim(self):
        action = self.create_action()
        self.assertFalse(p.revalidate_action(action, fresh_for(action))["allowed"])
        fresh = claimed_fresh(action)
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])
        for key in ("durable_intent_persisted", "atomic_once_only_claim"):
            invalid = deepcopy(fresh)
            invalid["creation_claim"][key] = False
            self.assertFalse(p.revalidate_action(action, invalid)["allowed"])

    def test_guard_rejects_wrong_action_operation_and_journal_fingerprint(self):
        action = self.create_action()
        for key in ("action_id", "operation_id", "prepared_ledger_fingerprint", "prepared_generation", "from_status", "to_status"):
            with self.subTest(key=key):
                fresh = claimed_fresh(action)
                fresh["creation_claim"][key] = "incorrect"
                self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_guard_rejects_journal_changes_beyond_new_intent(self):
        action = self.create_action(self.known_and_new())
        fresh = claimed_fresh(action)
        fresh["managed_context"]["ledger"]["operations"].pop(0)
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_guard_rejects_replay_after_uncertain_attempt(self):
        action = self.create_action()
        fresh = claimed_fresh(action)
        fresh["managed_context"]["ledger"]["operations"][-1]["status"] = "uncertain"
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])
        data = self.source_data()
        data["managed_context"] = deepcopy(fresh["managed_context"])
        self.no_create(data, "previously_issued_marker_requires_verified_mapping")

    def test_guard_rejects_changed_writer_or_missing_ledger_reread(self):
        action = self.create_action()
        fresh = claimed_fresh(action)
        fresh["managed_context"]["single_writer"]["token"] = "another-owner"
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])
        for key in ("all_ledger_destination_ids", "ledger_and_single_writer_reread"):
            fresh = claimed_fresh(action)
            fresh["reread_certificate"][key] = False
            self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_guard_rejects_changed_tracked_snapshot(self):
        action = self.create_action(self.known_and_new())
        fresh = claimed_fresh(action)
        fresh["marker_inventory"]["tracked_destination_observations"][0]["event"]["fields"]["description"] = "erased"
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_guard_rejects_new_unrelated_owned_or_malformed_observation(self):
        action = self.create_action()
        for extra_text in ("", " damaged"):
            fresh = claimed_fresh(action)
            target = mirror(event("new-orphan"))
            target["fields"]["description"] += extra_text
            fresh["marker_inventory"]["observed_marker_events"].append(found(B, target))
            self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_second_run_after_verified_create_is_idempotent(self):
        source, target, mapping = paired()
        result = p.plan(managed(projection(batch([source], [target], [mapping]))))
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 1, "conflict": 0})

    def test_missing_ledger_does_not_change_existing_verified_noops(self):
        source, target, mapping = paired()
        data = managed(projection(batch([source], [target], [mapping])))
        del data["managed_context"]
        self.assertEqual(p.plan(data)["counts"]["noop"], 1)

    def test_universal_mode_remains_supported_without_managed_certificates(self):
        action = writes(p.plan(projection(batch([event()]))))[0]
        self.assertTrue(p.revalidate_action(action, fresh_for(action))["allowed"])

    def test_universal_scope_does_not_bypass_supplied_issued_history(self):
        data = self.source_data()
        search = data["calendars"][1]["marker_searches"][0]
        search["scope"] = p.UNIVERSAL_SCOPE
        del search["queries"]
        action = self.create_action(data)
        self.assertTrue(p.revalidate_action(action, claimed_fresh(action))["allowed"])
        ledger = data["managed_context"]["ledger"]
        ledger["issued"] = [{"marker": search["marker"], "destination_calendar_id": B, "destination_ids": [], "disposition": "retired"}]
        ledger["operations"] = [{"operation_id": "old", "marker": search["marker"], "status": "verified_no_write", "action_id": None}]
        self.no_create(data, "previously_issued_marker_requires_verified_mapping")

    def test_operation_history_cannot_be_omitted_or_relabelled(self):
        data = self.known_and_new()
        data["managed_context"]["ledger"]["operations"] = []
        self.no_create(data, "issued_markers_require_operation_history")
        data = self.known_and_new()
        data["managed_context"]["ledger"]["operations"][0]["status"] = "verified_no_write"
        self.no_create(data, "mapped_marker_requires_committed_operation_history")

    def test_changed_ledger_invalidates_other_create_from_same_plan(self):
        data = managed(projection(batch([event("one"), event("two")])) )
        actions = writes(p.plan(data))
        fresh = claimed_fresh(actions[1])
        fresh["managed_context"]["ledger"]["issued"].append({"marker": actions[0]["marker"],
            "destination_calendar_id": B, "destination_ids": [], "disposition": "unresolved"})
        fresh["managed_context"]["ledger"]["operations"].append({"operation_id": "other", "marker": actions[0]["marker"],
            "status": "attempt_started", "action_id": actions[0]["id"]})
        self.assertFalse(p.revalidate_action(actions[1], fresh)["allowed"])

    def test_managed_plan_and_guard_do_not_mutate_inputs(self):
        data = self.source_data()
        old = deepcopy(data)
        action = self.create_action(data)
        self.assertEqual(data, old)
        fresh = claimed_fresh(action)
        old_action, old_fresh = deepcopy(action), deepcopy(fresh)
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])
        self.assertEqual(action, old_action)
        self.assertEqual(fresh, old_fresh)

    def test_279_validated_migrated_markers_plus_new_source(self):
        sources, targets, mappings = [], [], []
        for index in range(279):
            source, target, mapping = paired(event(f"source-{index}"), mirror(event(f"source-{index}"), event_id=f"mirror-{index}"))
            sources.append(source)
            targets.append(target)
            mappings.append(mapping)
        sources.append(event("new-never-issued"))
        result = p.plan(managed(projection(batch(sources, targets, mappings))))
        self.assertEqual(result["counts"], {"create": 1, "update": 0, "delete": 0, "noop": 279, "conflict": 0})


class SerializedRunnerTests(unittest.TestCase):
    def data(self):
        return serialized(managed(projection(batch([event()]))))

    def action(self):
        result = p.plan(self.data())
        self.assertEqual(result["counts"]["create"], 1, result)
        return writes(result)[0]

    def test_production_can_create_without_cas_or_atomic_claim(self):
        action = self.action()
        self.assertFalse(action["expected"]["connector_capabilities"]["conditional_writes"])
        self.assertEqual(action["write_contract"]["creation_attempt_mode"], "serialized_runner")
        self.assertNotIn("durable_intent_then_atomic_once_only_attempt_claim_required", action["write_contract"])
        fresh = claimed_fresh(action)
        self.assertNotIn("atomic_once_only_claim", fresh["creation_claim"])
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])

    def test_root_coordination_paused_drained_and_one_admission_all_required(self):
        for flag in ("root_coordinated", "cutover_paused_and_drained", "only_one_execution_admitted"):
            with self.subTest(flag=flag):
                data = self.data()
                data["managed_context"]["single_writer"][flag] = False
                self.assertEqual(p.plan(data)["counts"]["create"], 0)

    def test_both_durable_readbacks_and_uncalled_uninterrupted_execution_required(self):
        action = self.action()
        for flag in ("intent_readback_verified", "attempt_started_readback_verified",
                     "same_uninterrupted_admitted_execution", "calendar_call_not_yet_attempted", "durable_intent_persisted"):
            with self.subTest(flag=flag):
                fresh = claimed_fresh(action)
                fresh["creation_claim"][flag] = False
                self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_resume_or_retry_after_crash_is_not_authorized(self):
        action = self.action()
        fresh = claimed_fresh(action)
        fresh["creation_claim"]["resume_or_retry"] = True
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_restart_with_started_or_uncertain_ledger_cannot_replan_create(self):
        action = self.action()
        for status in ("prepared", "attempt_started", "uncertain"):
            with self.subTest(status=status):
                data = self.data()
                data["managed_context"] = claimed_fresh(action)["managed_context"]
                data["managed_context"]["ledger"]["operations"][-1]["status"] = status
                self.assertEqual(p.plan(data)["counts"]["create"], 0)

    def test_different_root_admission_cannot_continue_prior_attempt(self):
        action = self.action()
        fresh = claimed_fresh(action)
        fresh["managed_context"]["single_writer"]["token"] = "new-root-admission"
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_declared_modes_cannot_be_swapped_in_prewrite_record(self):
        action = self.action()
        fresh = claimed_fresh(action)
        fresh["creation_claim"]["mode"] = "atomic_claim"
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])
        fresh = claimed_fresh(action)
        fresh["creation_claim"]["atomic_once_only_claim"] = True
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_attempt_started_must_be_actual_new_ledger_transition(self):
        action = self.action()
        fresh = claimed_fresh(action)
        fresh["managed_context"]["ledger"]["operations"][-1]["status"] = "prepared"
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_stateless_identical_replay_is_explicitly_not_prevented(self):
        action = self.action()
        fresh = claimed_fresh(action)
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])
        # The second result is not a second authorization to call Calendar. Root
        # must consume its in-memory one-call path and never replay after crash.

    def test_explicit_atomic_mode_remains_optional_and_valid(self):
        data = managed(projection(batch([event()])))
        data["managed_context"]["single_writer"]["mode"] = "atomic_claim"
        action = writes(p.plan(data))[0]
        self.assertEqual(action["write_contract"]["creation_attempt_mode"], "atomic_claim")
        self.assertTrue(p.revalidate_action(action, claimed_fresh(action))["allowed"])


def configured_batch(config, paired_events=False):
    config = p.validate_config(config)
    first, second = p.calendar_ids(config)
    source = event("portable-source")
    result = projection(batch([source]))
    result["config"] = config
    result["state"]["config_fingerprint"] = p.config_fingerprint(config)
    for row, cid in zip(result["calendars"], (first, second)):
        row["id"] = cid
    source = result["calendars"][0]["listing"]["events"][0]
    marker = p.marker_for(config, first, source["id"], None, second)
    result["calendars"][1]["marker_searches"][0]["marker"] = marker
    if paired_events:
        target = deepcopy(source)
        target.update(id="portable-mirror", status="confirmed", self_response="none")
        target["fields"] = p.mirror_fields(marker, "google_calendar_projection")
        result["calendars"][1]["listing"]["events"] = [target]
        result["state"]["mappings"] = [{"source": {"calendar_id": first, "event_id": source["id"], "original_start_time": None},
            "destination": p.ref(second, target["id"]), "marker": marker, "verified_source": deepcopy(source), "verified_destination": deepcopy(target)}]
        result["details"] = {"complete": True, "requested": [p.ref(first, source["id"]), p.ref(second, target["id"])],
                             "responses": [found(first, source), found(second, target)]}
    return result


class RuntimeConfigTests(unittest.TestCase):
    def other_config(self):
        return {"version": 1, "namespace": "fedcba9876543210fedcba9876543210", "calendars": [
            {"calendar_id": "other-calendar-one@example.invalid", "self_identities": ["owner-one@example.invalid"], "self_identities_verified": True},
            {"calendar_id": "other-calendar-two@example.invalid", "self_identities": ["owner-two@example.invalid"], "self_identities_verified": True}]}

    def test_identical_source_code_accepts_unrelated_configs(self):
        for cfg in (CONFIG, self.other_config()):
            with self.subTest(namespace=cfg["namespace"]):
                data = serialized(managed(configured_batch(cfg)))
                result = p.plan(data)
                self.assertEqual(result["counts"]["create"], 1, result)
                self.assertEqual(result["config_fingerprint"], p.config_fingerprint(cfg))
                self.assertTrue(p.revalidate_action(writes(result)[0], claimed_fresh(writes(result)[0]))["allowed"])
                self.assertEqual(p.plan(serialized(managed(configured_batch(cfg, True))))["counts"]["noop"], 1)

    def test_interleaved_concurrent_configs_have_no_global_leakage(self):
        from concurrent.futures import ThreadPoolExecutor
        datasets = [serialized(managed(configured_batch(cfg))) for cfg in (CONFIG, self.other_config())]
        expected = [p.plan(data)["plan_id"] for data in datasets]
        with ThreadPoolExecutor(max_workers=4) as pool:
            observed = list(pool.map(lambda i: p.plan(datasets[i % 2])["plan_id"], range(16)))
        self.assertEqual(observed, [expected[i % 2] for i in range(16)])

    def test_config_is_required_no_environment_default_or_primary_fallback(self):
        data = configured_batch(CONFIG)
        del data["config"]
        self.assertEqual(p.plan(data)["counts"]["create"], 0)
        for alias in ("primary", "PRIMARY", "default", "personal", "", " spaced "):
            cfg = deepcopy(CONFIG)
            cfg["calendars"][0]["calendar_id"] = alias
            with self.assertRaises(p.ContractError):
                p.validate_config(cfg)

    def test_config_requires_exactly_two_distinct_explicit_calendars(self):
        for rows in ([], CONFIG["calendars"][:1], CONFIG["calendars"] * 2, [CONFIG["calendars"][0]] * 2):
            cfg = deepcopy(CONFIG)
            cfg["calendars"] = deepcopy(rows)
            with self.assertRaises(p.ContractError):
                p.validate_config(cfg)

    def test_namespace_and_verified_identity_validation(self):
        for namespace in (None, "", "A" * 32, "0" * 31, "0" * 33, "[.*]"):
            cfg = deepcopy(CONFIG)
            cfg["namespace"] = namespace
            with self.assertRaises(p.ContractError):
                p.validate_config(cfg)
        for identities, verified in (([], True), (["not-an-email"], True), ([A, A.upper()], True), ([A], False)):
            cfg = deepcopy(CONFIG)
            cfg["calendars"][0].update(self_identities=identities, self_identities_verified=verified)
            with self.assertRaises(p.ContractError):
                p.validate_config(cfg)

    def test_config_reordering_and_email_case_are_canonical_but_calendar_ids_stay_raw(self):
        cfg = deepcopy(CONFIG)
        cfg["calendars"].reverse()
        cfg["calendars"][0]["self_identities"][0] = B.upper()
        self.assertEqual(p.config_fingerprint(cfg), p.config_fingerprint(CONFIG))
        cfg["calendars"][0]["calendar_id"] = B.upper()
        self.assertNotEqual(p.config_fingerprint(cfg), p.config_fingerprint(CONFIG))

    def test_same_calendar_namespace_but_other_verified_identity_rejects_state(self):
        data = configured_batch(CONFIG, True)
        data["config"]["calendars"][0]["self_identities"] = ["another-owner@example.invalid"]
        result = p.plan(data)
        self.assertEqual(result["counts"]["noop"], 0)
        self.assertEqual(result["actions"][0]["reason"], "state_config_mismatch")

    def test_cross_namespace_or_calendar_state_cannot_be_rebound_implicitly(self):
        for cfg in (self.other_config(), {**deepcopy(CONFIG), "namespace": "f" * 32}):
            data = configured_batch(cfg, True)
            data["state"] = configured_batch(CONFIG, True)["state"]
            self.assertEqual(p.plan(data)["status"], "blocked")

    def test_missing_state_binding_is_not_filled_in(self):
        data = configured_batch(CONFIG)
        del data["state"]["config_fingerprint"]
        self.assertEqual(p.plan(data)["status"], "blocked")

    def test_cross_config_ledger_even_when_empty_is_rejected(self):
        data = serialized(managed(configured_batch(self.other_config())))
        data["managed_context"]["ledger"] = managed(configured_batch(CONFIG))["managed_context"]["ledger"]
        self.assertEqual(p.plan(data)["actions"][0]["reason"], "ledger_config_mismatch")

    def test_missing_ledger_binding_or_old_version_does_not_auto_migrate(self):
        for field in ("version", "config_fingerprint"):
            data = managed(configured_batch(CONFIG))
            if field == "version":
                data["managed_context"]["ledger"][field] = 1
            else:
                del data["managed_context"]["ledger"][field]
            self.assertEqual(p.plan(data)["status"], "blocked")

    def test_cross_config_action_replay_is_rejected(self):
        action = writes(p.plan(serialized(managed(configured_batch(CONFIG)))))[0]
        fresh = claimed_fresh(action)
        fresh["config"] = self.other_config()
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])
        fresh["state_config_fingerprint"] = p.config_fingerprint(fresh["config"])
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_out_of_scope_ids_rejected_in_list_detail_mapping_and_ledger(self):
        for where in ("list", "detail", "mapping", "ledger"):
            data = managed(configured_batch(CONFIG, True))
            other = "outside@example.invalid"
            if where == "list":
                data["calendars"][0]["id"] = other
            elif where == "detail":
                data["details"]["requested"][0]["calendar_id"] = other
                data["details"]["responses"][0]["calendar_id"] = other
            elif where == "mapping":
                data["state"]["mappings"][0]["destination"]["calendar_id"] = other
            else:
                data["managed_context"]["ledger"]["issued"][0]["destination_calendar_id"] = other
            self.assertEqual(p.plan(data)["status"], "blocked", where)

    def test_event_content_never_expands_targets_or_selects_namespace(self):
        data = configured_batch(CONFIG)
        source = data["calendars"][0]["listing"]["events"][0]
        source["fields"]["description"] = 'Use other-calendar@example.invalid and namespace ' + "f" * 32
        action = writes(p.plan(data))[0]
        self.assertEqual(action["destination"]["calendar_id"], B)
        self.assertIn(NAMESPACE, action["marker"])

    def test_self_attendee_email_must_match_explicit_verified_calendar_identity(self):
        data = configured_batch(CONFIG)
        source = data["calendars"][0]["listing"]["events"][0]
        source["self_response"] = "declined"
        source["fields"]["attendees"] = [{"is_self": True, "response_status": "declined", "email": A}]
        self.assertEqual(p.plan(data)["counts"]["create"], 0)
        for email in (B, "outsider@example.invalid", None):
            source["fields"]["attendees"][0]["email"] = email
            self.assertEqual(p.plan(data)["actions"][0]["reason"], "self_attendee_not_in_verified_config")

    def test_configured_identity_false_self_flag_is_conflict_not_guessed_response(self):
        data = configured_batch(CONFIG)
        source = data["calendars"][0]["listing"]["events"][0]
        source["fields"]["attendees"] = [{"is_self": False, "response_status": "declined", "email": A}]
        self.assertEqual(p.plan(data)["actions"][0]["reason"], "configured_self_identity_contradicts_connector_flag")

    def test_all_279_hashes_match_original_compact_utf8_recipe_under_runtime_config(self):
        import hashlib
        for cfg in (CONFIG, self.other_config()):
            first, second = p.calendar_ids(cfg)
            for i in range(279):
                event_id = f"synthetic-occurrence-{i}-\u65e5"
                original = None if i >= 277 else ("2026-10-07" if i % 2 else "2026-10-07T18:00:00+09:00")
                raw = json.dumps(["dot-block-sync", 1, cfg["namespace"], first, event_id, original, second], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                expected = "[dot-block-sync:v1:" + cfg["namespace"] + ":" + hashlib.sha256(raw).hexdigest() + "]"
                self.assertEqual(p.marker_for(cfg, first, event_id, original, second), expected)


class DistributionScanTests(unittest.TestCase):
    def test_nested_archive_and_binary_utf16_deny_term_are_inspected(self):
        import distribution_check
        import zipfile
        canary = "reserved-synthetic-private-canary"
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("synthetic.bin", canary.encode("utf-16-le"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outer.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("nested.zip", inner.getvalue())
            report = distribution_check.scan([path], [canary])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["archives_inspected"], 2)
        self.assertTrue(any(item["path"].endswith("synthetic.bin") for item in report["findings"]))
        self.assertNotIn(canary, json.dumps(report))

    def test_clean_synthetic_config_has_no_generic_private_findings(self):
        import distribution_check
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(CONFIG), encoding="utf-8")
            report = distribution_check.scan([path])
        self.assertEqual(report["findings"], [])

    def test_manifest_detects_unlisted_archive_file(self):
        import distribution_check
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("package/MANIFEST.json", json.dumps({"files": []}))
                archive.writestr("package/unlisted.txt", "synthetic")
            report = distribution_check.scan([path])
        self.assertIn("manifest_inventory_mismatch", [f["rule"] for f in report["findings"]])


def recovery_batch(data=None):
    """Synthetic root-certified zero-call failure; no external evidence is read."""
    data = deepcopy(data) if data is not None else serialized(managed(projection(batch([event()], responses=[found(A, event())]))))
    prior = writes(p.plan(data))[0]
    failed = claimed_fresh(prior)
    data["managed_context"] = deepcopy(failed["managed_context"])
    data["managed_context"]["single_writer"]["token"] = "synthetic-root-admission-recovery"
    ledger = data["managed_context"]["ledger"]
    ledger["generation"] = "synthetic-recovery-approved"
    old = ledger["operations"][-1]
    ledger["recoveries"] = [{"recovery_id": "synthetic-recovery-1", "operation_id": old["operation_id"],
        "marker": old["marker"], "outcome": "aborted_before_call", "operation_fingerprint": p.digest(old),
        "prior_action": deepcopy(prior), "config_fingerprint": p.config_fingerprint(CONFIG),
        "observations_fingerprint": p.recovery_observation_fingerprint(data),
        "authorized_admission_token": data["managed_context"]["single_writer"]["token"],
        "evidence": {"kind": "executor_trace", "trace_reference": "synthetic-closed-execution-trace",
            "trace_sha256": p.digest("synthetic-zero-call-trace"),
            "prior_admission_token": prior["expected"]["managed_context"]["single_writer"]["token"],
            "durable": True, "trace_complete": True, "execution_closed": True, "calendar_calls_issued": 0,
            "call_dispatch_started": False, "external_write_uncertainty": False}, "consumed_by_operation_id": None}]
    return data


def preflight_fresh(action):
    fresh = fresh_for(action)
    if "managed_context" in action["expected"]:
        fresh["managed_context"] = deepcopy(action["expected"]["managed_context"])
        fresh["reread_certificate"].update(all_ledger_destination_ids=True, ledger_and_single_writer_reread=True)
    return fresh


def supersede_unused(data=None):
    """Synthetic audited append only; production must obtain real root evidence."""
    data = deepcopy(data) if data is not None else recovery_batch()
    context = data["managed_context"]
    ledger = context["ledger"]
    old = ledger["recoveries"][-1]
    suffix = str(len(ledger["recoveries"]) + 1)
    context["single_writer"]["token"] = "synthetic-refresh-admission-" + suffix
    ledger["generation"] = "synthetic-refresh-generation-" + suffix
    new = {"recovery_id": "synthetic-refreshed-certificate-" + suffix, "operation_id": old["operation_id"],
        "marker": old["marker"], "observations_scope": p.RECOVERY_ITEM_SCOPE,
        "observations_fingerprint": p.recovery_observation_fingerprint(data, old["marker"]),
        "authorized_admission_token": context["single_writer"]["token"], "consumed_by_operation_id": None,
        "supersession": {"recovery_id": old["recovery_id"], "recovery_fingerprint": p.digest(old),
            "reason": "refresh_current_observations", "audit_reference": "synthetic-unused-certificate-audit-" + suffix,
            "audit_sha256": p.digest("synthetic-closed-unused-admission-" + suffix),
            "root_authorized": True, "previous_admission_closed": True, "certificate_unused_verified": True}}
    ledger["recoveries"].append(new)
    return data


class RecoverySupersessionTests(unittest.TestCase):
    def no_writes(self, data):
        result = p.plan(data)
        self.assertEqual(writes(result), [], result)

    def test_unused_refresh_is_compact_audited_and_preserves_old_certificate(self):
        data = recovery_batch()
        old = deepcopy(data["managed_context"]["ledger"])
        data["state"]["generation"] += "-fresh-read"
        self.no_writes(data)
        refreshed = supersede_unused(data)
        ledger = refreshed["managed_context"]["ledger"]
        self.assertEqual(ledger["recoveries"][:-1], old["recoveries"])
        self.assertEqual(ledger["operations"], old["operations"])
        self.assertEqual(ledger["issued"], old["issued"])
        self.assertNotIn("prior_action", ledger["recoveries"][-1])
        self.assertNotIn("evidence", ledger["recoveries"][-1])
        self.assertLess(len(p.canonical(ledger["recoveries"][-1]).encode("utf-8")), 1600)
        action = writes(p.plan(refreshed))[0]
        self.assertEqual(action["expected"]["recovery"], ledger["recoveries"][-1])
        self.assertTrue(p.preflight_action(action, preflight_fresh(action))["ready_to_prepare"])
        fresh = claimed_fresh(action)
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])
        self.assertEqual(fresh["managed_context"]["ledger"]["recoveries"][:-1], old["recoveries"])

    def test_legacy_records_and_one_argument_fingerprint_remain_compatible(self):
        data = recovery_batch()
        self.assertEqual(len(writes(p.plan(data))), 1)
        self.assertEqual(p.recovery_observation_fingerprint(data), p.digest({"config": p.validate_config(data["config"]),
            **{key: data[key] for key in ("run_started_at", "connector_capabilities", "calendars", "details", "state")}}))

    def test_new_initial_certificate_can_use_item_scope_without_supersession(self):
        data = recovery_batch()
        record = data["managed_context"]["ledger"]["recoveries"][0]
        record["observations_scope"] = p.RECOVERY_ITEM_SCOPE
        record["observations_fingerprint"] = p.recovery_observation_fingerprint(data, record["marker"])
        self.assertEqual(len(writes(p.plan(data))), 1)

    def test_legacy_near_file_limit_grows_by_small_references_only(self):
        data = recovery_batch()
        ledger = data["managed_context"]["ledger"]
        record = ledger["recoveries"][0]
        prior = record["prior_action"]
        prior["notes"] = ["synthetic large historical snapshot " + "x" * 860000]
        prior["id"] = p.digest({k: v for k, v in prior.items() if k != "id"})
        ledger["operations"][0]["action_id"] = prior["id"]
        record["operation_fingerprint"] = p.digest(ledger["operations"][0])
        original_size = len(json.dumps(ledger, indent=2).encode("utf-8"))
        self.assertGreater(original_size, 860000)
        for _ in range(10):
            data = supersede_unused(data)
        current = data["managed_context"]["ledger"]
        self.assertLess(len(json.dumps(current, indent=2).encode("utf-8")) - original_size, 20000)
        self.assertLess(len(json.dumps(current, indent=2).encode("utf-8")), 1000000)
        self.assertEqual(current["recoveries"][0], record)
        self.assertEqual(len(writes(p.plan(data))), 1)

    def test_each_supersession_audit_field_is_required_and_truthful(self):
        valid = supersede_unused()
        audit = valid["managed_context"]["ledger"]["recoveries"][-1]["supersession"]
        for key in audit:
            with self.subTest(missing=key):
                data = deepcopy(valid)
                del data["managed_context"]["ledger"]["recoveries"][-1]["supersession"][key]
                self.no_writes(data)
        for key, value in {"audit_reference": "", "audit_sha256": "bad", "root_authorized": False,
                           "previous_admission_closed": False, "certificate_unused_verified": False,
                           "reason": "retry_after_empty_search", "recovery_fingerprint": "f" * 64}.items():
            with self.subTest(changed=key):
                data = deepcopy(valid)
                data["managed_context"]["ledger"]["recoveries"][-1]["supersession"][key] = value
                self.no_writes(data)

    def test_same_admission_cannot_refresh_an_unused_certificate(self):
        data = supersede_unused()
        context = data["managed_context"]
        new, old = context["ledger"]["recoveries"][-1], context["ledger"]["recoveries"][0]
        context["single_writer"]["token"] = new["authorized_admission_token"] = old["authorized_admission_token"]
        self.no_writes(data)

    def test_supersession_cannot_reuse_an_earlier_nonadjacent_admission(self):
        data = supersede_unused(supersede_unused())
        context = data["managed_context"]
        records = context["ledger"]["recoveries"]
        context["single_writer"]["token"] = records[-1]["authorized_admission_token"] = records[0]["authorized_admission_token"]
        self.no_writes(data)

    def test_compact_supersession_cannot_target_a_different_operation_or_marker(self):
        for key, value in (("operation_id", "different-operation"), ("marker", p.marker_for(CONFIG, A, "another-source", None, B))):
            data = supersede_unused()
            data["managed_context"]["ledger"]["recoveries"][-1][key] = value
            self.no_writes(data)

    def test_consumed_certificate_cannot_be_refreshed_even_before_call(self):
        data = recovery_batch()
        action = writes(p.plan(data))[0]
        data["managed_context"] = claimed_fresh(action)["managed_context"]
        refreshed = supersede_unused(data)
        self.no_writes(refreshed)

    def test_uncertain_committed_or_positive_call_evidence_cannot_refresh(self):
        for change in ("uncertain", "committed", "call", "dispatch", "uncertainty"):
            data = supersede_unused()
            ledger = data["managed_context"]["ledger"]
            original = ledger["recoveries"][0]
            if change in {"uncertain", "committed"}:
                ledger["operations"][0]["status"] = change
                original["operation_fingerprint"] = p.digest(ledger["operations"][0])
            else:
                key, value = {"call": ("calendar_calls_issued", 1), "dispatch": ("call_dispatch_started", True),
                              "uncertainty": ("external_write_uncertainty", True)}[change]
                original["evidence"][key] = value
            ledger["recoveries"][-1]["supersession"]["recovery_fingerprint"] = p.digest(original)
            self.no_writes(data)

    def test_origin_evidence_cannot_be_overridden_in_compact_certificate(self):
        for key, value in (("prior_action", {}), ("evidence", {}), ("outcome", "verified_no_write"), ("config_fingerprint", "f" * 64)):
            data = supersede_unused()
            data["managed_context"]["ledger"]["recoveries"][-1][key] = value
            self.no_writes(data)

    def test_refresh_must_reference_latest_existing_same_operation_and_preserve_chain(self):
        valid = supersede_unused(supersede_unused())
        for change in ("branch", "missing", "cycle", "deleted_origin", "deleted_middle", "changed_origin", "duplicate"):
            with self.subTest(change=change):
                data = deepcopy(valid)
                records = data["managed_context"]["ledger"]["recoveries"]
                if change == "branch":
                    records[-1]["supersession"].update(recovery_id=records[0]["recovery_id"], recovery_fingerprint=p.digest(records[0]))
                elif change in {"missing", "cycle"}:
                    records[-1]["supersession"]["recovery_id"] = "unknown" if change == "missing" else records[-1]["recovery_id"]
                elif change == "deleted_origin":
                    del records[0]
                elif change == "deleted_middle":
                    del records[1]
                elif change == "changed_origin":
                    records[0]["observations_fingerprint"] = "f" * 64
                else:
                    records.append(deepcopy(records[-1]))
                self.no_writes(data)

    def test_old_action_and_old_certificate_cannot_execute_after_supersession(self):
        data = recovery_batch()
        old_action = writes(p.plan(data))[0]
        refreshed = supersede_unused(data)
        current_action = writes(p.plan(refreshed))[0]
        self.assertFalse(p.preflight_action(old_action, preflight_fresh(current_action))["ready_to_prepare"])
        self.assertFalse(p.revalidate_action(old_action, claimed_fresh(current_action))["allowed"])
        fresh = claimed_fresh(current_action)
        # Trying to consume the superseded origin changes its referenced hash.
        records = fresh["managed_context"]["ledger"]["recoveries"]
        records[0]["consumed_by_operation_id"] = records[-1]["consumed_by_operation_id"]
        records[-1]["consumed_by_operation_id"] = None
        fresh["managed_context"]["ledger"]["operations"][-1]["recovery_id"] = records[0]["recovery_id"]
        self.assertFalse(p.revalidate_action(current_action, fresh)["allowed"])

    def test_refreshed_certificate_consumes_only_its_own_new_attempt_once(self):
        data = supersede_unused(supersede_unused())
        action = writes(p.plan(data))[0]
        fresh = claimed_fresh(action)
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])
        records = fresh["managed_context"]["ledger"]["recoveries"]
        self.assertTrue(all(record["consumed_by_operation_id"] is None for record in records[:-1]))
        self.assertIsNotNone(records[-1]["consumed_by_operation_id"])
        data["managed_context"] = fresh["managed_context"]
        self.no_writes(data)


class ScopedRecoveryObservationTests(unittest.TestCase):
    def data(self):
        relevant, unrelated = event(), event("unrelated-native")
        data = recovery_batch(serialized(managed(projection(batch([relevant], responses=[found(A, relevant)])))))
        extra = projection(batch([unrelated], responses=[found(A, unrelated)]))
        data["calendars"][0]["listing"]["events"].extend(extra["calendars"][0]["listing"]["events"])
        data["details"]["requested"].extend(extra["details"]["requested"])
        data["details"]["responses"].extend(extra["details"]["responses"])
        return supersede_unused(data)

    def target(self, data):
        marker = data["managed_context"]["ledger"]["recoveries"][-1]["marker"]
        return next((action for action in p.plan(data)["actions"] if action.get("marker") == marker), None)

    def test_unrelated_native_attachment_change_keeps_target_action_and_both_guards(self):
        before = self.data()
        action = self.target(before)
        after = deepcopy(before)
        for observation in (after["calendars"][0]["listing"]["events"][1], after["details"]["responses"][1]["event"]):
            observation["fields"]["other"]["observed"]["attachments"] = [{"file_url": "https://example.invalid/changed-attachment"}]
        marker = action["marker"]
        self.assertEqual(p.recovery_observation_fingerprint(before, marker), p.recovery_observation_fingerprint(after, marker))
        fresh_action = self.target(after)
        self.assertEqual(action, fresh_action)
        self.assertTrue(p.preflight_action(action, preflight_fresh(fresh_action))["ready_to_prepare"])
        self.assertTrue(p.revalidate_action(action, claimed_fresh(fresh_action))["allowed"])

    def test_source_attachment_time_and_identity_changes_still_block(self):
        for change in ("attachment", "time", "identity"):
            data = self.data()
            for source in (data["calendars"][0]["listing"]["events"][0], data["details"]["responses"][0]["event"]):
                if change == "attachment":
                    source["fields"]["other"]["observed"]["attachments"] = ["changed"]
                elif change == "time":
                    source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
                else:
                    source["original_start_time"] = "2026-10-07T18:00:00+09:00"
            target = self.target(data)
            self.assertTrue(target is None or target["op"] != "create")

    def test_relevant_change_cannot_be_approved_away_by_supersession(self):
        data = self.data()
        for source in (data["calendars"][0]["listing"]["events"][0], data["details"]["responses"][0]["event"]):
            source["fields"]["summary"] += " changed"
        refreshed = supersede_unused(data)
        self.assertNotEqual(self.target(refreshed)["op"], "create")

    def test_unrelated_event_becoming_owned_or_malformed_invalidates_ownership_inventory(self):
        for suffix in ("", " extra"):
            data = self.data()
            marker = data["managed_context"]["ledger"]["recoveries"][-1]["marker"]
            for source in (data["calendars"][0]["listing"]["events"][1], data["details"]["responses"][1]["event"]):
                source["fields"]["description"] = marker + suffix
            self.assertNotEqual(self.target(data)["op"], "create")

    def test_destination_match_and_marker_query_candidate_changes_still_block(self):
        for location in ("window", "query"):
            data = self.data()
            destination = projection(batch([], [mirror(event())]))["calendars"][1]["listing"]["events"][0]
            if location == "window":
                data["calendars"][1]["listing"]["events"].append(destination)
            else:
                data["calendars"][1]["marker_searches"][0]["events"].append(destination)
            self.assertNotEqual(self.target(data)["op"], "create")

    def test_coverage_config_state_ledger_and_unknown_scope_changes_fail_closed(self):
        for change in ("pages", "details", "queries", "config", "state", "ledger", "scope"):
            data = self.data()
            if change == "pages":
                data["calendars"][1]["listing"]["complete"] = False
            elif change == "details":
                data["details"]["responses"].pop()
            elif change == "queries":
                data["calendars"][1]["marker_searches"][0]["queries"][0]["complete"] = False
            elif change == "config":
                data["config"]["namespace"] = "f" * 32
            elif change == "state":
                data["state"]["generation"] += "-changed"
            elif change == "ledger":
                data["managed_context"]["ledger"]["generation"] += "-changed"
            else:
                data["managed_context"]["ledger"]["recoveries"][-1]["observations_scope"] = "ignore_all_changes"
            with self.subTest(change=change):
                target = self.target(data)
                self.assertTrue(target is None or target["op"] != "create")
                if change in {"pages", "details", "queries", "config", "scope"}:
                    with self.assertRaises(p.ContractError):
                        p.recovery_observation_fingerprint(data, data["managed_context"]["ledger"]["recoveries"][-1]["marker"])


class RecoveryTests(unittest.TestCase):
    def no_writes(self, data):
        result = p.plan(data)
        self.assertEqual(writes(result), [], result)
        return result

    def test_zero_call_recovery_creates_new_action_and_preserves_all_history(self):
        data = recovery_batch()
        original = deepcopy(data)
        action = writes(p.plan(data))[0]
        self.assertEqual(action["reason"], "verified_zero_call_recovery_new_attempt")
        prior = data["managed_context"]["ledger"]["recoveries"][0]["prior_action"]
        self.assertNotEqual(action["id"], prior["id"])
        fresh = claimed_fresh(action)
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])
        before, after = data["managed_context"]["ledger"], fresh["managed_context"]["ledger"]
        self.assertEqual(before["issued"], after["issued"])
        self.assertEqual(before["operations"], after["operations"][:-1])
        self.assertEqual(after["operations"][-1]["recovery_id"], before["recoveries"][0]["recovery_id"])
        self.assertEqual(after["recoveries"][0]["consumed_by_operation_id"], after["operations"][-1]["operation_id"])
        self.assertEqual(data, original)

    def test_empty_search_and_status_label_do_not_prove_no_write(self):
        for status in ("attempt_started", "verified_no_write", "prepared", "aborted_before_call"):
            with self.subTest(status=status):
                data = recovery_batch()
                ledger = data["managed_context"]["ledger"]
                del ledger["recoveries"]
                ledger["operations"][0]["status"] = status
                self.no_writes(data)

    def test_every_zero_call_evidence_field_is_required(self):
        for key in recovery_batch()["managed_context"]["ledger"]["recoveries"][0]["evidence"]:
            with self.subTest(key=key):
                data = recovery_batch()
                del data["managed_context"]["ledger"]["recoveries"][0]["evidence"][key]
                self.no_writes(data)

    def test_actual_call_unknown_outcome_open_execution_or_search_only_cannot_recover(self):
        changes = {"kind": "empty_marker_search", "trace_reference": "", "trace_sha256": "bad",
                   "durable": False, "trace_complete": False, "execution_closed": False,
                   "calendar_calls_issued": 1, "call_dispatch_started": True, "external_write_uncertainty": True}
        for key, value in changes.items():
            with self.subTest(key=key):
                data = recovery_batch()
                data["managed_context"]["ledger"]["recoveries"][0]["evidence"][key] = value
                self.no_writes(data)
        for value in (False, 0.0, "0", None, -1):
            data = recovery_batch()
            data["managed_context"]["ledger"]["recoveries"][0]["evidence"]["calendar_calls_issued"] = value
            self.no_writes(data)

    def test_uncertain_and_committed_operations_cannot_be_relabelled_by_recovery(self):
        for status in ("uncertain", "committed"):
            data = recovery_batch()
            ledger = data["managed_context"]["ledger"]
            ledger["operations"][0]["status"] = status
            ledger["recoveries"][0]["operation_fingerprint"] = p.digest(ledger["operations"][0])
            self.no_writes(data)

    def test_explicit_verified_no_write_and_aborted_outcomes_require_same_full_proof(self):
        for status in ("aborted_before_call", "verified_no_write", "prepared"):
            data = recovery_batch()
            ledger = data["managed_context"]["ledger"]
            ledger["operations"][0]["status"] = status
            ledger["recoveries"][0]["operation_fingerprint"] = p.digest(ledger["operations"][0])
            ledger["recoveries"][0]["outcome"] = "verified_no_write"
            self.assertEqual(len(writes(p.plan(data))), 1)

    def test_source_changed_even_with_new_observation_certificate_blocks(self):
        for path in ("time", "description", "status"):
            data = recovery_batch()
            source = data["calendars"][0]["listing"]["events"][0]
            if path == "time":
                source["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
            elif path == "description":
                source["fields"]["description"] = "changed after attempt"
            else:
                source["status"] = "tentative"
            data["details"]["responses"][0]["event"] = deepcopy(source)
            data["managed_context"]["ledger"]["recoveries"][0]["observations_fingerprint"] = p.recovery_observation_fingerprint(data)
            self.no_writes(data)

    def test_any_new_observation_or_state_generation_requires_new_approval(self):
        data = recovery_batch()
        data["state"]["generation"] = "another-state-generation"
        self.no_writes(data)
        data = recovery_batch()
        data["calendars"][0]["listing"]["events"].append(projection(batch([event("unrelated")]))["calendars"][0]["listing"]["events"][0])
        self.no_writes(data)

    def test_recovery_requires_source_by_id_and_complete_current_reads(self):
        data = recovery_batch()
        data["details"] = {"complete": True, "requested": [], "responses": []}
        data["managed_context"]["ledger"]["recoveries"][0]["observations_fingerprint"] = p.recovery_observation_fingerprint(data)
        self.no_writes(data)
        for field in ("pages", "details", "queries"):
            data = recovery_batch()
            if field == "pages":
                data["calendars"][1]["listing"]["complete"] = False
            elif field == "details":
                data["details"]["complete"] = False
            else:
                data["calendars"][1]["marker_searches"][0]["queries"][0]["complete"] = False
            self.no_writes(data)

    def test_exact_duplicate_and_suspect_mirror_block_recovery(self):
        for text in (None, " manual suffix"):
            data = recovery_batch()
            target = projection(batch([], [mirror(event())]))["calendars"][1]["listing"]["events"][0]
            if text:
                target["fields"]["description"] += text
            data["calendars"][1]["listing"]["events"] = [target]
            data["managed_context"]["ledger"]["recoveries"][0]["observations_fingerprint"] = p.recovery_observation_fingerprint(data)
            self.no_writes(data)

    def test_config_fingerprint_and_admission_cannot_be_rebound(self):
        for change in ("config", "state", "ledger", "recovery", "writer", "old_token", "same_token", "atomic"):
            data = recovery_batch()
            context = data["managed_context"]
            rec = context["ledger"]["recoveries"][0]
            if change == "config":
                data["config"]["namespace"] = "f" * 32
            elif change in {"state", "recovery", "ledger"}:
                (data["state"] if change == "state" else rec if change == "recovery" else context["ledger"])["config_fingerprint"] = "f" * 64
            elif change == "writer":
                context["single_writer"]["token"] = "another-admission"
            elif change == "old_token":
                rec["evidence"]["prior_admission_token"] = "another-prior-admission"
            elif change == "same_token":
                rec["authorized_admission_token"] = rec["evidence"]["prior_admission_token"]
                context["single_writer"]["token"] = rec["authorized_admission_token"]
            else:
                context["single_writer"] = {"mode": "atomic_claim", "externally_enforced": True, "token": rec["authorized_admission_token"]}
            with self.subTest(change=change):
                self.no_writes(data)

    def test_mismatched_operation_action_and_approval_fingerprints_block(self):
        for key in ("operation_fingerprint", "observations_fingerprint"):
            data = recovery_batch()
            data["managed_context"]["ledger"]["recoveries"][0][key] = "f" * 64
            self.no_writes(data)
        data = recovery_batch()
        data["managed_context"]["ledger"]["recoveries"][0]["prior_action"]["expected"]["destination"]["evidence"] = None
        self.no_writes(data)

    def test_recovery_is_consumed_once_and_cannot_be_reopened_or_old_attempt_replayed(self):
        data = recovery_batch()
        action = writes(p.plan(data))[0]
        fresh = claimed_fresh(action)
        data["managed_context"] = fresh["managed_context"]
        self.no_writes(data)
        data["managed_context"]["ledger"]["recoveries"][0]["consumed_by_operation_id"] = None
        self.no_writes(data)
        fresh = claimed_fresh(action)
        fresh["creation_claim"]["operation_id"] = action["expected"]["recovery"]["operation_id"]
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_duplicate_approvals_and_reusing_one_for_multiple_operations_fail(self):
        data = recovery_batch()
        ledger = data["managed_context"]["ledger"]
        duplicate = deepcopy(ledger["recoveries"][0])
        duplicate["recovery_id"] += "-second"
        ledger["recoveries"].append(duplicate)
        self.no_writes(data)
        data = recovery_batch()
        action = writes(p.plan(data))[0]
        fresh = claimed_fresh(action)
        duplicate = deepcopy(fresh["managed_context"]["ledger"]["operations"][-1])
        duplicate["operation_id"] += "-second"
        fresh["managed_context"]["ledger"]["operations"].append(duplicate)
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_guard_rejects_cleared_old_operation_issued_or_recovery_history(self):
        action = writes(p.plan(recovery_batch()))[0]
        for name in ("operations", "issued", "recoveries"):
            fresh = claimed_fresh(action)
            del fresh["managed_context"]["ledger"][name][0]
            self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_guard_rejects_unlinked_consumption_changed_source_and_changed_config(self):
        action = writes(p.plan(recovery_batch()))[0]
        for change in ("link", "consumption", "source", "config"):
            fresh = claimed_fresh(action)
            if change == "link":
                del fresh["managed_context"]["ledger"]["operations"][-1]["recovery_id"]
            elif change == "consumption":
                fresh["managed_context"]["ledger"]["recoveries"][0]["consumed_by_operation_id"] = None
            elif change == "source":
                fresh["source"]["event"]["fields"]["summary"] += " changed"
            else:
                fresh["config"]["namespace"] = "f" * 32
            self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_second_failure_requires_a_new_linked_recovery_record(self):
        data = recovery_batch()
        action = writes(p.plan(data))[0]
        data["managed_context"] = claimed_fresh(action)["managed_context"]
        ledger = data["managed_context"]["ledger"]
        previous = deepcopy(ledger["recoveries"][0])
        new = deepcopy(previous)
        new.update(recovery_id="synthetic-recovery-2", operation_id=ledger["operations"][-1]["operation_id"],
                   operation_fingerprint=p.digest(ledger["operations"][-1]), prior_action=deepcopy(action),
                   authorized_admission_token="synthetic-third-admission", consumed_by_operation_id=None)
        new["evidence"]["prior_admission_token"] = data["managed_context"]["single_writer"]["token"]
        new["evidence"]["trace_reference"] += "-second"
        new["evidence"]["trace_sha256"] = p.digest("synthetic-second-zero-call-trace")
        data["managed_context"]["single_writer"]["token"] = new["authorized_admission_token"]
        ledger["generation"] = "synthetic-second-recovery-approved"
        ledger["recoveries"].append(new)
        next_action = writes(p.plan(data))[0]
        self.assertTrue(p.revalidate_action(next_action, claimed_fresh(next_action))["allowed"])
        self.assertEqual(ledger["recoveries"][0], previous)


class PreflightTests(unittest.TestCase):
    def action(self):
        return writes(p.plan(recovery_batch()))[0]

    def test_preflight_checks_before_any_journal_mutation_and_never_authorizes_calendar(self):
        action = self.action()
        fresh = preflight_fresh(action)
        original = deepcopy(fresh)
        result = p.preflight_action(action, fresh)
        self.assertTrue(result["ready_to_prepare"], result)
        self.assertFalse(result["allowed"])
        self.assertFalse(result["calendar_call_allowed"])
        self.assertEqual(fresh, original)
        self.assertFalse(p.revalidate_action(action, fresh)["allowed"])

    def test_extra_evidence_null_on_marker_absent_fails_both_guards(self):
        action = self.action()
        for make, guard in ((preflight_fresh, p.preflight_action), (claimed_fresh, p.revalidate_action)):
            fresh = make(action)
            fresh["destination"]["evidence"] = None
            result = guard(action, fresh)
            self.assertFalse(result["allowed"])
            self.assertEqual(result["reason"], "destination_snapshot_mismatch")
            self.assertFalse(result.get("ready_to_prepare", False))

    def test_preflight_requires_unchanged_context_and_every_reread_certificate(self):
        action = self.action()
        fresh = preflight_fresh(action)
        fresh["managed_context"]["ledger"]["generation"] = "changed-generation"
        self.assertFalse(p.preflight_action(action, fresh)["ready_to_prepare"])
        for key in preflight_fresh(action)["reread_certificate"]:
            fresh = preflight_fresh(action)
            fresh["reread_certificate"][key] = False
            self.assertFalse(p.preflight_action(action, fresh)["ready_to_prepare"])

    def test_preflight_cannot_accept_already_started_claim(self):
        action = self.action()
        self.assertFalse(p.preflight_action(action, claimed_fresh(action))["ready_to_prepare"])

    def test_cli_preflight_exit_codes_and_malformed_requests(self):
        action = self.action()
        request = {"action": action, "fresh": preflight_fresh(action)}
        for data, code, ready in ((request, 0, True), ({"action": action, "fresh": {}}, 2, False)):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(json.dumps(data))), patch("sys.stdout", output):
                self.assertEqual(p.main(["--preflight"]), code)
            self.assertEqual(json.loads(output.getvalue())["ready_to_prepare"], ready)
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("malformed")), patch("sys.stdout", output):
            self.assertEqual(p.main(["--preflight"]), 2)
        self.assertFalse(json.loads(output.getvalue())["ready_to_prepare"])


def series_split_batch(count=3):
    old_id, new_id = "synthetic-old-master", "synthetic-new-master"
    old_uid, new_uid = "old-series@example.invalid", "new-series@example.invalid"
    sources, targets, mappings = [], [], []
    for index in range(count):
        begin = (p.instant("2026-10-07T09:00:00Z") + timedelta(days=7 * index)).astimezone(
            p.ZoneInfo("Asia/Tokyo")).isoformat()
        end = (p.instant(begin) + timedelta(hours=1)).astimezone(p.ZoneInfo("Asia/Tokyo")).isoformat()
        before = event("stable-instance-" + str(index), begin, end)
        before.update(recurring_event_id=old_id, original_start_time=begin)
        before["fields"]["other"]["i_cal_uid"] = old_uid
        target = mirror(before, event_id="stable-mirror-" + str(index))
        mappings.append(registry(before, target))
        after = deepcopy(before)
        after["recurring_event_id"] = new_id
        after["fields"]["other"]["i_cal_uid"] = new_uid
        sources.append(after)
        targets.append(target)
    data = serialized(managed(projection(batch(sources, targets, mappings))))
    organizer = {"email": A, "is_self": True}
    old = {"calendar_id": A, "event_id": old_id, "status": "confirmed", "organizer": deepcopy(organizer),
        "created": "2026-09-01T00:00:00Z", "updated": "2026-10-06T10:00:00Z",
        "start": "2026-09-23T18:00:00+09:00", "end": "2026-09-23T19:00:00+09:00", "time_zone": "Asia/Tokyo",
        "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20261007T085959Z"], "i_cal_uid": old_uid}
    new = deepcopy(old)
    new.update(event_id=new_id, created="2026-10-06T10:01:00Z", updated="2026-10-06T10:01:00Z",
               start="2026-10-07T18:00:00+09:00", end="2026-10-07T19:00:00+09:00",
               recurrence=["RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=" + str(count)], i_cal_uid=new_uid)
    old_rows = [{"calendar_id": A, "event_id": "before-split-" + str(index), "recurring_event_id": old_id,
        "original_start_time": begin, "status": "confirmed", "start": begin,
        "end": (p.instant(begin) + timedelta(hours=1)).astimezone(p.ZoneInfo("Asia/Tokyo")).isoformat(),
        "organizer": deepcopy(organizer), "i_cal_uid": old_uid}
        for index, begin in enumerate(("2026-09-23T18:00:00+09:00", "2026-09-30T18:00:00+09:00"))]
    new_rows = [{"calendar_id": A, "event_id": source["id"], "recurring_event_id": new_id,
        "original_start_time": source["original_start_time"], "status": "confirmed", "start": source["start"]["dateTime"],
        "end": source["end"]["dateTime"], "organizer": deepcopy(organizer), "i_cal_uid": new_uid} for source in sources]
    def collection(master_id, rows):
        boundary = max(1, len(rows) // 2)
        pages = [{"request_page_token": None, "response": {"events": rows[:boundary], "next_page_token": "synthetic-next-page"}},
                 {"request_page_token": "synthetic-next-page", "response": {"events": rows[boundary:], "next_page_token": None}}]
        return {"calendar_id": A, "master_id": master_id, "show_deleted": True, "time_min": None, "time_max": None,
                "complete": True, "pages": pages}
    certificate = {"version": 1, "kind": "following_events_split", "transition_id": "synthetic-reviewed-split",
        "config_fingerprint": p.config_fingerprint(CONFIG), "state_generation": data["state"]["generation"],
        "source_calendar_id": A, "split_original_start_time": new["start"], "old_master": old, "new_master": new,
        "old_instances": collection(old_id, old_rows), "new_instances": collection(new_id, new_rows), "occurrences": [],
        "review": {"status": "approved", "connector": "google_calendar_direct", "evidence_reference": "synthetic-readonly-split-audit",
            "evidence_sha256": p.digest("synthetic-full-master-instance-and-mirror-readbacks"), "readonly_evidence_verified": True,
            "previous_writers_drained": True, "admission_token": data["managed_context"]["single_writer"]["token"],
            "timestamp_relation": "new_created_equals_updated"}}
    return attach_series_certificate(data, certificate)


def attach_series_certificate(data, certificate):
    data, certificate = deepcopy(data), deepcopy(certificate)
    data.pop("series_transitions", None)
    observer = p.Planner(data)
    observer.validate()
    certificate["occurrences"] = []
    for key, mapping in sorted(observer.mappings.items()):
        if mapping["verified_source"]["recurring_event_id"] != certificate["old_master"]["event_id"]:
            continue
        destination = observer.source_snapshot(p.pair_ref(observer.config, mapping["destination"]))
        certificate["occurrences"].append({"source_event_id": key[1], "original_start_time": mapping["source"]["original_start_time"],
            "marker": mapping["marker"], "destination": deepcopy(mapping["destination"]), "mapping_fingerprint": p.digest(mapping),
            "source_fingerprint": p.digest(observer.source_snapshot(key)), "destination_fingerprint": p.digest(destination)})
    data["series_transitions"] = [certificate]
    return data


def series_fresh(data):
    return {"data": deepcopy(data), "reread_certificate": {key: True for key in (
        "complete", "immediately_before_state_write", "source_and_destination_ids", "masters_and_instance_pages",
        "state_and_ledger", "single_writer")}}


class SeriesTransitionTests(unittest.TestCase):
    def blocked(self, data):
        result = p.plan(data)
        self.assertEqual(writes(result), [], result)
        self.assertEqual(result.get("series_rebinds", []), [], result)
        self.assertIn(result["status"], {"blocked", "review_required"})
        return result

    def test_reviewed_thirteen_instance_split_only_rebinds_verified_source_baselines(self):
        data = series_split_batch(13)
        original = deepcopy(data)
        result = p.plan(data)
        self.assertEqual(result["status"], "series_rebind_ready", result)
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 13, "conflict": 0})
        self.assertFalse(result["calendar_call_allowed"])
        proposal = result["series_rebinds"][0]
        self.assertEqual(len(proposal["replacements"]), 13)
        before = {m["marker"]: m for m in data["state"]["mappings"]}
        for after in proposal["replacements"]:
            old = before[after["marker"]]
            self.assertEqual({k: v for k, v in after.items() if k != "verified_source"},
                             {k: v for k, v in old.items() if k != "verified_source"})
            self.assertEqual(after["verified_source"]["recurring_event_id"], "synthetic-new-master")
            self.assertEqual(after["marker"], p.marker_for(CONFIG, A, after["source"]["event_id"], after["source"]["original_start_time"], B))
            self.assertNotEqual(after["verified_source"]["fields"]["other"]["observed"]["i_cal_uid"],
                                old["verified_source"]["fields"]["other"]["observed"]["i_cal_uid"])
        self.assertEqual(data, original)

    def test_without_reviewed_certificate_series_id_change_still_conflicts(self):
        data = series_split_batch()
        del data["series_transitions"]
        result = self.blocked(data)
        self.assertTrue(all(a["reason"] == "source_series_identity_changed" for a in result["actions"]))

    def test_state_only_guard_cannot_authorize_calendar_or_accept_mutation_guard(self):
        data = series_split_batch()
        proposal = p.plan(data)["series_rebinds"][0]
        checked = p.revalidate_series_rebind(proposal, series_fresh(data))
        self.assertTrue(checked["state_write_allowed"], checked)
        self.assertFalse(checked["allowed"])
        self.assertFalse(checked["calendar_call_allowed"])
        self.assertFalse(p.revalidate_action(proposal, {})["allowed"])

    def test_idempotent_after_state_only_adoption_preserves_ledger_and_mirrors(self):
        data = series_split_batch()
        ledger = deepcopy(data["managed_context"]["ledger"])
        result = p.plan(data)
        data["state"]["mappings"] = result["series_rebinds"][0]["replacements"]
        data["state"]["generation"] += "-rebound"
        del data["series_transitions"]
        after = p.plan(data)
        self.assertEqual(after["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 3, "conflict": 0})
        self.assertEqual(data["managed_context"]["ledger"], ledger)

    def test_partial_or_unreviewed_certificate_and_unverified_state_block(self):
        for path in ("review", "scope", "state", "config", "source_calendar", "writer", "audit_hash", "timestamp"):
            data = series_split_batch()
            cert = data["series_transitions"][0]
            if path == "review":
                cert["review"]["readonly_evidence_verified"] = False
            elif path == "scope":
                cert["review"]["connector"] = "unverified_export"
            elif path == "state":
                data["state"]["status"] = "uncertain"
            elif path == "config":
                cert["config_fingerprint"] = "f" * 64
            elif path == "source_calendar":
                cert["source_calendar_id"] = "third@example.invalid"
            elif path == "writer":
                cert["review"]["admission_token"] += "-wrong"
            elif path == "audit_hash":
                cert["review"]["evidence_sha256"] = "bad"
            else:
                cert["new_master"]["updated"] = "2026-10-06T10:02:00Z"
            with self.subTest(path=path):
                self.blocked(data)

    def test_every_certificate_top_level_field_is_required(self):
        for key in series_split_batch()["series_transitions"][0]:
            data = series_split_batch()
            del data["series_transitions"][0][key]
            self.blocked(data)

    def test_self_ownership_must_match_verified_calendar_identities(self):
        for role in ("old_master", "new_master", "old_instances", "new_instances"):
            for change in ("self", "email"):
                data = series_split_batch()
                value = data["series_transitions"][0][role]
                if "pages" in value:
                    value = value["pages"][0]["response"]["events"][0]
                value["organizer"]["is_self" if change == "self" else "email"] = False if change == "self" else "not-owner@example.invalid"
                self.blocked(data)

    def test_all_pages_tokens_show_deleted_and_unbounded_requests_are_required(self):
        for role in ("old_instances", "new_instances"):
            for change in ("incomplete", "deleted", "bounded", "unread", "token", "empty", "extra_page"):
                data = series_split_batch()
                collection = data["series_transitions"][0][role]
                if change == "incomplete":
                    collection["complete"] = False
                elif change == "deleted":
                    collection["show_deleted"] = False
                elif change == "bounded":
                    collection["time_min"] = RUN
                elif change == "unread":
                    collection["pages"].pop()
                elif change == "token":
                    collection["pages"][1]["request_page_token"] = "wrong-page"
                elif change == "empty":
                    collection["pages"] = []
                else:
                    collection["pages"].append(deepcopy(collection["pages"][-1]))
                with self.subTest(role=role, change=change):
                    self.blocked(data)

    def test_cancelled_duplicates_missing_and_multiple_candidates_are_conflicts(self):
        for role in ("old_instances", "new_instances"):
            for change in ("cancelled", "duplicate", "alias", "missing", "master", "uid"):
                data = series_split_batch()
                rows = data["series_transitions"][0][role]["pages"][0]["response"]["events"]
                if change == "cancelled":
                    rows[0]["status"] = "cancelled"
                elif change in {"duplicate", "alias"}:
                    rows.append(deepcopy(rows[0]))
                    if change == "alias":
                        rows[-1]["event_id"] += "-alias"
                elif change == "missing":
                    rows.pop()
                elif change == "master":
                    rows[0]["recurring_event_id"] = "wrong-master"
                else:
                    rows[0]["i_cal_uid"] = "wrong-uid@example.invalid"
                self.blocked(data)

    def test_truncation_boundary_schedule_and_supported_rule_are_verified(self):
        changes = (("old_master", "recurrence", ["RRULE:FREQ=WEEKLY;UNTIL=20261007T090000Z"]),
                   ("new_master", "recurrence", ["RRULE:FREQ=DAILY;COUNT=3"]),
                   ("new_master", "recurrence", ["RRULE:FREQ=WEEKLY"]),
                   ("new_master", "recurrence", ["RRULE:FREQ=WEEKLY;BYDAY=WE;COUNT=2"]),
                   ("new_master", "end", "2026-10-07T20:00:00+09:00"),
                   ("new_master", "time_zone", "Etc/UTC"),
                   ("new_master", "start", "2026-10-14T18:00:00+09:00"),
                   ("new_master", "start", "2026-10-07"))
        for role, field, value in changes:
            data = series_split_batch()
            data["series_transitions"][0][role][field] = value
            self.blocked(data)

    def test_raw_original_identity_must_be_byte_identical_not_just_same_instant(self):
        data = series_split_batch()
        current = data["calendars"][0]["listing"]["events"][0]
        current["original_start_time"] = "2026-10-07T09:00:00Z"
        data["details"]["responses"][0]["event"] = deepcopy(current)
        cert = data["series_transitions"][0]
        cert["new_instances"]["pages"][0]["response"]["events"][0]["original_start_time"] = current["original_start_time"]
        data = attach_series_certificate(data, cert)
        self.blocked(data)

    def test_changed_current_times_never_trigger_calendar_update_from_this_mode(self):
        data = series_split_batch()
        current = data["calendars"][0]["listing"]["events"][0]
        current["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
        data["details"]["responses"][0]["event"] = deepcopy(current)
        cert = data["series_transitions"][0]
        cert["new_instances"]["pages"][0]["response"]["events"][0]["start"] = current["start"]["dateTime"]
        self.blocked(attach_series_certificate(data, cert))

    def test_manual_mirror_title_marker_time_location_attendees_and_extra_fields_block(self):
        for change in ("title", "marker", "time", "location", "attendees", "extra"):
            data = series_split_batch()
            target = data["calendars"][1]["listing"]["events"][0]
            if change == "title":
                target["fields"]["summary"] = "manual"
            elif change == "marker":
                target["fields"]["description"] += " manual"
            elif change == "time":
                target["start"]["dateTime"] = "2026-10-07T17:00:00+09:00"
            elif change == "location":
                target["fields"]["location"] = "manual"
            elif change == "attendees":
                target["fields"]["attendees"] = [{"email": "guest@example.invalid", "is_self": False, "response_status": "accepted"}]
            else:
                target["fields"]["other"]["observed"]["color_id"] = "manual"
            data["details"]["responses"][1]["event"] = deepcopy(target)
            self.blocked(attach_series_certificate(data, data["series_transitions"][0]))

    def test_mirror_duplicate_or_missing_destination_blocks_whole_proposal(self):
        data = series_split_batch()
        extra = deepcopy(data["calendars"][1]["listing"]["events"][0])
        extra["id"] = "another-mirror-id"
        data["calendars"][1]["listing"]["events"].append(extra)
        self.blocked(data)
        data = series_split_batch()
        data["calendars"][1]["listing"]["events"].pop(0)
        data["details"]["responses"][1] = terminal(B, "stable-mirror-0", "not_found")
        self.blocked(data)

    def test_full_occurrence_set_and_source_ids_are_required(self):
        for change in ("missing", "duplicate", "wrong_source", "wrong_destination", "marker", "fingerprint"):
            data = series_split_batch()
            rows = data["series_transitions"][0]["occurrences"]
            if change == "missing":
                rows.pop()
            elif change == "duplicate":
                rows[-1] = deepcopy(rows[0])
            elif change == "wrong_source":
                rows[0]["source_event_id"] = "not-registered"
            elif change == "wrong_destination":
                rows[0]["destination"]["event_id"] = "not-owned"
            elif change == "marker":
                rows[0]["marker"] = rows[1]["marker"]
            else:
                rows[0]["source_fingerprint"] = "f" * 64
            self.blocked(data)

    def test_foreign_calendar_in_master_or_page_cannot_rebind(self):
        for role in ("old_master", "new_master", "old_instances", "new_instances"):
            data = series_split_batch()
            data["series_transitions"][0][role]["calendar_id"] = B
            self.blocked(data)

    def test_missing_or_unmapped_ledger_entry_cannot_be_adopted(self):
        data = series_split_batch()
        data["managed_context"]["ledger"]["issued"][0]["disposition"] = "unresolved"
        self.blocked(data)

    def test_old_instance_page_cannot_contradict_known_id_tombstone(self):
        data = series_split_batch()
        row = data["series_transitions"][0]["old_instances"]["pages"][0]["response"]["events"][0]
        data["details"]["requested"].append(p.ref(A, row["event_id"]))
        data["details"]["responses"].append(terminal(A, row["event_id"], "cancelled"))
        self.blocked(data)

    def test_already_moved_exception_retains_raw_original_and_current_times(self):
        data = series_split_batch()
        begin, end = "2026-10-08T18:00:00+09:00", "2026-10-08T19:00:00+09:00"
        for value in (data["calendars"][0]["listing"]["events"][0], data["calendars"][1]["listing"]["events"][0],
                      data["details"]["responses"][0]["event"], data["details"]["responses"][1]["event"],
                      data["state"]["mappings"][0]["verified_source"], data["state"]["mappings"][0]["verified_destination"]):
            value["start"]["dateTime"], value["end"]["dateTime"] = begin, end
        cert = data["series_transitions"][0]
        cert["new_instances"]["pages"][0]["response"]["events"][0].update(start=begin, end=end)
        data = attach_series_certificate(data, cert)
        result = p.plan(data)
        self.assertEqual(result["status"], "series_rebind_ready", result)
        self.assertEqual(writes(result), [])
        self.assertEqual({m["marker"] for m in result["series_rebinds"][0]["replacements"]},
                         {m["marker"] for m in data["state"]["mappings"]})

    def test_new_master_until_rule_and_independent_page_order_are_supported(self):
        data = series_split_batch()
        cert = data["series_transitions"][0]
        cert["new_master"]["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=WE;UNTIL=20261021T090000Z"]
        cert["new_instances"]["pages"][1]["response"]["events"].reverse()
        result = p.plan(data)
        self.assertEqual(result["status"], "series_rebind_ready", result)

    def test_duplicate_or_overlapping_transition_certificates_fail_closed(self):
        for same_id in (True, False):
            data = series_split_batch()
            duplicate = deepcopy(data["series_transitions"][0])
            if not same_id:
                duplicate["transition_id"] += "-second"
            data["series_transitions"].append(duplicate)
            self.blocked(data)

    def test_unrelated_calendar_mutations_are_withheld_in_series_state_review_mode(self):
        data = series_split_batch()
        extra = projection(batch([event("unrelated-busy")]))
        data["calendars"][0]["listing"]["events"].extend(extra["calendars"][0]["listing"]["events"])
        data["calendars"][1]["marker_searches"].extend(extra["calendars"][1]["marker_searches"])
        result = self.blocked(data)
        self.assertIn("calendar_mutations_deferred_during_series_transition_review", [a["reason"] for a in result["actions"]])

    def test_revalidation_checks_fresh_master_pages_state_ledger_and_all_reread_flags(self):
        data = series_split_batch()
        proposal = p.plan(data)["series_rebinds"][0]
        for key in series_fresh(data)["reread_certificate"]:
            fresh = series_fresh(data)
            fresh["reread_certificate"][key] = False
            self.assertFalse(p.revalidate_series_rebind(proposal, fresh)["state_write_allowed"])
        for change in ("state", "ledger", "master", "page", "proposal"):
            fresh, candidate = series_fresh(data), deepcopy(proposal)
            if change == "state":
                fresh["data"]["state"]["generation"] += "-changed"
            elif change == "ledger":
                fresh["data"]["managed_context"]["ledger"]["generation"] += "-changed"
            elif change == "master":
                fresh["data"]["series_transitions"][0]["new_master"]["updated"] = "2026-10-06T10:02:00Z"
            elif change == "page":
                fresh["data"]["series_transitions"][0]["new_instances"]["pages"].pop()
            else:
                candidate["replacements"][0]["marker"] = "tampered"
                candidate["id"] = p.digest({k: v for k, v in candidate.items() if k != "id"})
            self.assertFalse(p.revalidate_series_rebind(candidate, fresh)["state_write_allowed"])

    def test_cli_series_guard_distinguishes_state_permission_from_calendar_permission(self):
        data = series_split_batch()
        request = {"proposal": p.plan(data)["series_rebinds"][0], "fresh": series_fresh(data)}
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(request))), patch("sys.stdout", output):
            self.assertEqual(p.main(["--revalidate-series"]), 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["state_write_allowed"])
        self.assertFalse(result["allowed"])
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("bad-json")), patch("sys.stdout", output):
            self.assertEqual(p.main(["--revalidate-series"]), 2)
        self.assertFalse(json.loads(output.getvalue())["state_write_allowed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
