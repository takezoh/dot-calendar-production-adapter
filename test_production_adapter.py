"""Synthetic-only production adapter integration tests; no network/calendar data."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import planner as p
import production_adapter as a

from test_planner import CONFIG, A, B, NAMESPACE
RUN = "2026-10-06T12:00:00Z"
END = (p.instant(RUN) + timedelta(days=90)).isoformat()


def detail(event_id="native-1", start="2026-10-07T18:00:00+09:00", end="2026-10-07T19:00:00+09:00"):
    value = {key: None for key in a.DETAIL_FIELDS}
    value.update(id=event_id, summary="Synthetic event", description="", status="confirmed", start=start, end=end,
                 attendees=[], transparency=None, visibility="default", event_type="default",
                 reminders={"use_default": True, "overrides": None})
    return value


def search(event):
    value = {key: deepcopy(event[key]) for key in a.SEARCH_FIELDS - {"my_response_status"}}
    value["summary"] = value["summary"] or ""
    responses = [person["response_status"] for person in event["attendees"] if person["is_self"] is True]
    value["my_response_status"] = responses[0] if responses else None
    return value


def found(event):
    return {"event_id": event["id"], "outcome": "found", "event": deepcopy(event), "evidence": None}


def terminal(event_id, outcome):
    return {"event_id": event_id, "outcome": outcome, "event": None,
            "evidence": {"kind": outcome, "verified_known_id": True, "proof": "synthetic-terminal-proof"}
            if outcome in {"deleted", "cancelled"} else None}


def target(source, calendar=A, event_id="mirror-1"):
    value = deepcopy(source)
    value.update(id=event_id, summary="blocked", description=p.marker_for(CONFIG, calendar, source["id"],
                 source["original_start_time"], B if calendar == A else A), status="confirmed", visibility="private",
                 transparency="opaque", event_type="default", attendees=[], location=None, hangout_link=None,
                 recurring_event_id=None, original_start_time=None, reminders={"use_default": False, "overrides": None})
    return value


def timezone_proof(name="Asia/Tokyo"):
    return {"time_zone": name, "verified": True, "evidence": "synthetic-effective-event-zone"}


def mapping(source, destination, calendar=A, zone=None):
    return {"source": {"calendar_id": calendar, "event_id": source["id"], "original_start_time": source["original_start_time"]},
            "destination": {"calendar_id": B if calendar == A else A, "event_id": destination["id"]},
            "marker": destination["description"], "verified_source": a.normalize_event(CONFIG, calendar, source, zone),
            "verified_destination": a.normalize_event(CONFIG, B if calendar == A else A, destination, zone)}


def pages(events):
    return [{"request_page_token": None, "response": {"events": [search(e) for e in events], "next_page_token": None}}]


def split_batches(responses):
    return [{"requested_ids": [r["event_id"] for r in responses[i:i+10]], "complete": True,
             "responses": deepcopy(responses[i:i+10])} for i in range(0, len(responses), 10)]


def raw_input(events_a=None, events_b=None, mappings=None, extra_a=None, extra_b=None, zones_a=None, zones_b=None):
    calendars = []
    for cid, events, extras, zones in ((A, events_a or [], extra_a or [], zones_a or {}),
                                      (B, events_b or [], extra_b or [], zones_b or {})):
        calendars.append({"calendar_id": cid,
                          "listing": {"complete": True, "expanded": True, "includes_ongoing": True,
                                      "time_min": RUN, "time_max": END, "pages": pages(events)},
                          "detail_batches": split_batches([*[found(e) for e in events], *extras]),
                          "marker_searches": [], "all_day_timezones": deepcopy(zones)})
    return {"adapter_schema_version": a.RAW_SCHEMA_VERSION, "config": deepcopy(CONFIG), "run_started_at": RUN, "calendars": calendars,
            "state": {"mode": "planner", "value": {"config_fingerprint": p.config_fingerprint(CONFIG), "status": "verified", "generation": "synthetic-registry-1",
                       "mappings_complete": True, "mappings": deepcopy(mappings or [])}}}


def add_marker_lookup(data, source, source_calendar=A, candidates=None, complete=True):
    destination_calendar = B if source_calendar == A else A
    row = next(c for c in data["calendars"] if c["calendar_id"] == destination_calendar)
    row["marker_searches"].append({"marker": p.marker_for(CONFIG, source_calendar, source["id"], source["original_start_time"], destination_calendar),
        "scope": "exact_marker_all_destinations", "complete": complete, "coverage_verified": complete,
        "evidence": "synthetic-exhaustive-marker-lookup" if complete else None, "pages": pages(candidates or [])})


def legacy_registry(source, destination, calendar=A):
    source_tag, destination_tag = ("LEFT", "RIGHT") if calendar == A else ("RIGHT", "LEFT")
    marker = destination["description"]
    return {"namespace": NAMESPACE, "phase": "active", "owner": None, "errors": [], "updated_at": RUN,
            "config": {"calendars": {"LEFT": {"calendar_id": A}, "RIGHT": {"calendar_id": B}}},
            "records": [{"state": "verified", "error": None, "source": source_tag, "destination": destination_tag,
                         "source_event_id": source["id"], "destination_event_id": destination["id"],
                         "source_original_start_time": source["original_start_time"], "source_recurring_event_id": source["recurring_event_id"],
                         "start": source["start"], "end": source["end"], "all_day": bool(p.DAY.fullmatch(source["start"])),
                         "source_status": source["status"], "source_response_status": search(source)["my_response_status"],
                         "marker": marker, "key": marker[-65:-1], "last_verified_at": RUN}]}


def use_legacy(data, registry):
    data["state"] = {"mode": "legacy_bootstrap", "complete": True,
                     "registry_json": json.dumps(registry, ensure_ascii=False, indent=2) + "\n"}


def writes(result):
    return [item for item in result["actions"] if item["op"] in p.MUTATIONS]


class AdapterTests(unittest.TestCase):
    def blocked(self, raw, reason=None):
        with self.assertRaises(a.AdapterError) as caught:
            a.adapt(raw)
        if reason:
            self.assertEqual(str(caught.exception), reason)

    def test_new_source_with_truthful_marker_lookup_creates(self):
        source = detail()
        raw = raw_input([source])
        add_marker_lookup(raw, source)
        normalized = a.adapt(raw)
        action = writes(p.plan(normalized))[0]
        self.assertEqual(action["op"], "create")
        self.assertEqual(normalized["connector_capabilities"], a.CAPABILITIES)
        self.assertIsNone(action["expected"]["source"]["event"]["etag"])

    def test_missing_or_uncertified_marker_lookup_only_blocks_new_create(self):
        for mode in ("missing", "incomplete"):
            source = detail()
            raw = raw_input([source])
            if mode == "incomplete":
                add_marker_lookup(raw, source, complete=False)
            result = p.plan(a.adapt(raw))
            self.assertEqual(writes(result), [])
            self.assertIn("marker_search_not_certified_for_create_or_adoption", [x["reason"] for x in result["actions"]])

    def test_bidirectional_sources_keep_native_overlap(self):
        first, second = detail("native-a"), detail("native-b")
        raw = raw_input([first], [second])
        add_marker_lookup(raw, first, A)
        add_marker_lookup(raw, second, B)
        self.assertEqual(p.plan(a.adapt(raw))["counts"]["create"], 2)

    def test_verified_pair_noop_without_marker_lookup(self):
        source = detail()
        mirror = target(source)
        result = p.plan(a.adapt(raw_input([source], [mirror], [mapping(source, mirror)])))
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 1, "conflict": 0})

    def test_changed_source_updates_and_preserves_occurrence_key(self):
        source = detail()
        source.update(recurring_event_id="series", original_start_time="2026-10-07T18:00:00+09:00")
        mirror = target(source)
        stored = mapping(source, mirror)
        source.update(start="2026-10-09T18:00:00+09:00", end="2026-10-09T19:00:00+09:00",
                      original_start_time="2026-10-07T09:00:00Z")
        raw = raw_input([source], [mirror], [stored])
        normalized = a.adapt(raw)
        self.assertEqual(normalized["calendars"][0]["listing"]["events"][0]["original_start_time"], "2026-10-07T09:00:00Z")
        self.assertEqual(normalized["state"]["mappings"][0]["source"]["original_start_time"], "2026-10-07T18:00:00+09:00")
        action = writes(p.plan(normalized))[0]
        self.assertEqual(action["op"], "update")
        self.assertEqual(action["marker"], stored["marker"])
        self.assertEqual(action["destination"]["event_id"], mirror["id"])

    def test_missing_source_does_not_mean_delete(self):
        source = detail()
        mirror = target(source)
        raw = raw_input([], [mirror], [mapping(source, mirror)], extra_a=[terminal(source["id"], "not_found")])
        result = p.plan(a.adapt(raw))
        self.assertEqual(writes(result), [])
        self.assertEqual(result["counts"]["conflict"], 1)

    def test_explicit_known_id_terminal_evidence_reaches_planner(self):
        source = detail()
        mirror = target(source)
        for outcome in ("deleted", "cancelled"):
            raw = raw_input([], [mirror], [mapping(source, mirror)], extra_a=[terminal(source["id"], outcome)])
            action = writes(p.plan(a.adapt(raw)))[0]
            self.assertEqual(action["op"], "delete")
            self.assertEqual(action["expected"]["source"]["outcome"], outcome)

    def test_no_invented_terminal_evidence_for_partial_or_missing_batch_entry(self):
        source = detail()
        mirror = target(source)
        raw = raw_input([], [mirror], [mapping(source, mirror)], extra_a=[terminal(source["id"], "deleted")])
        raw["calendars"][0]["detail_batches"][0]["responses"][0]["evidence"] = None
        self.blocked(raw, "terminal_known_id_evidence_required")

    def test_known_source_moved_outside_window_uses_detail_read(self):
        source = detail()
        mirror = target(source)
        stored = mapping(source, mirror)
        source.update(start="2027-07-01T18:00:00+09:00", end="2027-07-01T19:00:00+09:00")
        raw = raw_input([], [mirror], [stored], extra_a=[found(source)])
        action = writes(p.plan(a.adapt(raw)))[0]
        self.assertEqual(action["reason"], "known_source_outside_future_window")

    def test_missing_registered_destination_is_not_recreated(self):
        source = detail()
        mirror = target(source)
        raw = raw_input([source], [], [mapping(source, mirror)], extra_b=[terminal(mirror["id"], "not_found")])
        self.assertEqual(writes(p.plan(a.adapt(raw))), [])

    def test_free_or_declined_source_is_explicitly_verified(self):
        for cause in ("free", "declined"):
            source = detail()
            mirror = target(source)
            stored = mapping(source, mirror)
            if cause == "free":
                source["transparency"] = "transparent"
            else:
                source["attendees"] = [{"is_self": True, "response_status": "declined", "email": A}]
            self.assertEqual(writes(p.plan(a.adapt(raw_input([source], [mirror], [stored]))))[0]["op"], "delete")

    def test_tentative_needs_action_is_still_busy(self):
        source = detail()
        source.update(status="tentative", attendees=[{"is_self": True, "response_status": "needsAction", "email": A}])
        raw = raw_input([source])
        add_marker_lookup(raw, source)
        self.assertEqual(p.plan(a.adapt(raw))["counts"]["create"], 1)

    def test_null_title_and_empty_search_title_are_same_untitled_event(self):
        source = detail()
        source["summary"] = None
        normalized = a.adapt(raw_input([source]))
        self.assertEqual(normalized["calendars"][0]["listing"]["events"][0]["fields"]["summary"], "")

    def test_null_is_self_and_null_overrides_are_normalized(self):
        source = detail()
        source["attendees"] = [{"is_self": None, "response_status": "needsAction", "email": "guest@example.invalid"}]
        normalized = a.normalize_event(CONFIG, A, source)
        self.assertIs(normalized["fields"]["attendees"][0]["is_self"], False)
        self.assertEqual(normalized["self_response"], "none")
        self.assertEqual(normalized["fields"]["reminders"], {"useDefault": True, "overrides": []})

    def test_nonnull_extra_properties_are_retained_even_when_falsy(self):
        source = detail()
        source.update(guests_can_modify=False, locked=False, color_id="0", event_label_id=0, attachments=[],
                      new_exposed_property={}, new_null_property=None)
        observed = a.normalize_event(CONFIG, A, source)["fields"]["other"]["observed"]
        self.assertEqual(observed, {"guests_can_modify": False, "locked": False, "color_id": "0", "event_label_id": 0,
                                    "attachments": [], "new_exposed_property": {}})

    def test_nonnull_new_extra_property_on_mirror_is_protected(self):
        source = detail()
        mirror = target(source)
        stored = mapping(source, mirror)
        mirror["new_exposed_property"] = "manual"
        result = p.plan(a.adapt(raw_input([source], [mirror], [stored])))
        self.assertEqual(writes(result), [])
        self.assertEqual(result["counts"]["conflict"], 1)

    def test_hangout_link_and_event_type_observation_are_preserved(self):
        source = detail()
        source["hangout_link"] = "https://meet.example.invalid/test"
        normalized = a.normalize_event(CONFIG, A, source)
        self.assertEqual(normalized["fields"]["hangout_link"], source["hangout_link"])
        self.assertEqual(normalized["fields"]["eventType"], "default")
        self.assertEqual(normalized["fields"]["conferenceData"], p.UNKNOWN)
        source["event_type"] = None
        self.assertEqual(a.normalize_event(CONFIG, A, source)["fields"]["eventType"], p.UNKNOWN)

    def test_full_required_detail_projection_cannot_be_filled_by_guessing(self):
        for key in a.DETAIL_FIELDS:
            source = detail()
            del source[key]
            with self.subTest(key=key), self.assertRaises(a.AdapterError):
                a.normalize_event(CONFIG, A, source)

    def test_listing_and_detail_change_fails_closed(self):
        raw = raw_input([detail()])
        raw["calendars"][0]["detail_batches"][0]["responses"][0]["event"]["start"] = "2026-10-07T17:00:00+09:00"
        self.blocked(raw, "event_changed_between_search_and_detail")

    def test_self_response_hint_must_match_full_attendee_read(self):
        raw = raw_input([detail()])
        raw["calendars"][0]["listing"]["pages"][0]["response"]["events"][0]["my_response_status"] = "accepted"
        self.blocked(raw, "self_response_changed_between_search_and_detail")

    def test_pagination_requires_contiguous_requests_and_final_null(self):
        first, second = detail("a"), detail("b")
        raw = raw_input([first, second])
        raw["calendars"][0]["listing"]["pages"] = [
            {"request_page_token": None, "response": {"events": [search(first)], "next_page_token": "next"}},
            {"request_page_token": "next", "response": {"events": [search(second)], "next_page_token": None}}]
        self.assertEqual(len(a.adapt(raw)["calendars"][0]["listing"]["events"]), 2)
        raw["calendars"][0]["listing"]["pages"][1]["request_page_token"] = "wrong"
        self.blocked(raw, "page_chain_gap_or_wrong_request_token")

    def test_incomplete_listing_and_unread_token_rejected(self):
        raw = raw_input([detail()])
        raw["calendars"][1]["listing"]["complete"] = False
        self.blocked(raw, "listing_completeness_or_expansion_not_certified")
        raw = raw_input([detail()])
        raw["calendars"][0]["listing"]["pages"][0]["response"]["next_page_token"] = "unread"
        self.blocked(raw, "unread_calendar_pages")

    def test_duplicate_events_and_token_loops_are_not_silently_deduplicated(self):
        raw = raw_input([detail()])
        events = raw["calendars"][0]["listing"]["pages"][0]["response"]["events"]
        events.append(deepcopy(events[0]))
        self.blocked(raw, "duplicate_or_invalid_search_event_id")
        raw = raw_input()
        raw["calendars"][0]["listing"]["pages"] = [
            {"request_page_token": None, "response": {"events": [], "next_page_token": "again"}},
            {"request_page_token": "again", "response": {"events": [], "next_page_token": "again"}}]
        self.blocked(raw, "pagination_token_loop")

    def test_batch_requests_above_ten_are_rejected_even_if_marked_complete(self):
        raw = raw_input([detail(str(index)) for index in range(11)])
        batches = raw["calendars"][0]["detail_batches"]
        batches[0]["requested_ids"] += batches[1]["requested_ids"]
        batches[0]["responses"] += batches[1]["responses"]
        batches.pop()
        self.blocked(raw, "detail_batch_must_request_1_to_10_ids")

    def test_ten_requested_nine_returned_is_partial_not_absence(self):
        raw = raw_input([detail(str(index)) for index in range(10)])
        raw["calendars"][0]["detail_batches"][0]["responses"].pop()
        self.blocked(raw, "requested_detail_ids_not_returned")

    def test_return_order_does_not_substitute_for_exact_id_matching(self):
        raw = raw_input([detail(str(index)) for index in range(10)])
        raw["calendars"][0]["detail_batches"][0]["responses"].reverse()
        self.assertEqual(len(a.adapt(raw)["details"]["responses"]), 10)
        raw["calendars"][0]["detail_batches"][0]["responses"][0]["event"]["id"] = "wrong"
        self.blocked(raw, "detail_id_or_evidence_mismatch")

    def test_duplicate_detail_requests_across_batches_rejected(self):
        raw = raw_input([detail()])
        raw["calendars"][0]["detail_batches"].append(deepcopy(raw["calendars"][0]["detail_batches"][0]))
        self.blocked(raw, "duplicate_detail_request_across_batches")

    def test_registered_id_omitted_from_requests_fails_closed(self):
        source = detail()
        mirror = target(source)
        raw = raw_input([source], [], [mapping(source, mirror)])
        self.blocked(raw, "planner_contract:registered_ids_require_explicit_detail_reads")

    def test_listing_candidate_needs_detail_even_if_unregistered(self):
        raw = raw_input([detail()])
        raw["calendars"][0]["detail_batches"] = []
        self.blocked(raw, "search_candidate_requires_successful_direct_detail_read")

    def test_declared_complete_marker_search_requires_coverage_evidence(self):
        source = detail()
        raw = raw_input([source])
        add_marker_lookup(raw, source)
        raw["calendars"][1]["marker_searches"][0]["coverage_verified"] = False
        self.blocked(raw, "complete_marker_lookup_requires_truthful_coverage_evidence")

    def test_marker_lookup_whole_description_prevents_substring_adoption(self):
        source = detail()
        mirror = target(source)
        mirror["description"] += " manual note"
        raw = raw_input([source], extra_b=[found(mirror)])
        add_marker_lookup(raw, source, candidates=[mirror])
        result = p.plan(a.adapt(raw))
        self.assertEqual(writes(result), [])
        self.assertIn("malformed_description_contains_matching_marker", [x["reason"] for x in result["actions"]])

    def test_all_day_uses_verified_zone_and_exclusive_date_end(self):
        source = detail(start="2026-10-07", end="2026-10-09")
        zone = timezone_proof()
        raw = raw_input([source], zones_a={source["id"]: zone})
        add_marker_lookup(raw, source)
        event = a.adapt(raw)["calendars"][0]["listing"]["events"][0]
        self.assertEqual(event["start"], {"date": "2026-10-07", "timeZone": "Asia/Tokyo"})
        self.assertEqual(event["end"], {"date": "2026-10-09", "timeZone": "Asia/Tokyo"})
        self.assertEqual(event["all_day_bounds"]["start"], "2026-10-07T00:00:00+09:00")

    def test_dst_midnights_are_resolved_independently_for_23_and_25_hour_days(self):
        for first, last, hours in (("2026-03-08", "2026-03-09", 23), ("2026-11-01", "2026-11-02", 25)):
            event = a.normalize_event(CONFIG, A, detail(start=first, end=last), timezone_proof("America/New_York"))
            self.assertEqual(p.bounds(event)[1] - p.bounds(event)[0], timedelta(hours=hours))

    def test_unavailable_or_unverified_all_day_zone_fails_closed(self):
        source = detail(start="2026-10-07", end="2026-10-08")
        with self.assertRaises(a.AdapterError):
            a.normalize_event(CONFIG, A, source)
        proof = timezone_proof()
        proof["verified"] = False
        with self.assertRaisesRegex(a.AdapterError, "all_day_timezone_not_verified"):
            a.normalize_event(CONFIG, A, source, proof)
        with self.assertRaisesRegex(a.AdapterError, "timezone_database_or_zone_unavailable"):
            a.normalize_event(CONFIG, A, source, timezone_proof("No/Such_Zone"))

    def test_nonexistent_and_ambiguous_midnight_fail_closed(self):
        with self.assertRaisesRegex(a.AdapterError, "all_day_midnight_nonexistent"):
            a.resolve_midnight("2011-12-30", "Pacific/Apia")
        with self.assertRaisesRegex(a.AdapterError, "all_day_midnight_ambiguous"):
            a.resolve_midnight("2026-11-01", "America/Havana")

    def test_mixed_date_and_datetime_and_invalid_duration_rejected(self):
        for start, end in (("2026-10-07", "2026-10-08T18:00:00+09:00"),
                           ("2026-10-07T18:00:00+09:00", "2026-10-07T18:00:00+09:00")):
            with self.assertRaises(a.AdapterError):
                a.normalize_event(CONFIG, A, detail(start=start, end=end))

    def test_legacy_bootstrap_is_explicit_and_not_a_persistence_write(self):
        source = detail()
        mirror = target(source)
        raw = raw_input([source], [mirror])
        registry = legacy_registry(source, mirror)
        use_legacy(raw, registry)
        before = deepcopy(raw)
        normalized = a.adapt(raw)
        self.assertEqual(raw, before)
        self.assertEqual(normalized["state"]["status"], "bootstrap")
        digest = hashlib.sha256(raw["state"]["registry_json"].encode("utf-8")).hexdigest()
        self.assertIn(digest, normalized["state"]["generation"])
        result = p.plan(normalized)
        self.assertEqual(result["status"], "bootstrap_ready")
        self.assertEqual(writes(result), [])

    def test_legacy_all_day_bootstrap_uses_caller_verified_zone(self):
        source = detail(start="2026-11-01", end="2026-11-02")
        mirror = target(source)
        proof = timezone_proof("America/New_York")
        raw = raw_input([source], [mirror], zones_a={source["id"]: proof}, zones_b={mirror["id"]: proof})
        use_legacy(raw, legacy_registry(source, mirror))
        self.assertEqual(p.plan(a.adapt(raw))["status"], "bootstrap_ready")

    def test_legacy_pending_owner_errors_partial_row_and_series_change_fail_closed(self):
        source = detail()
        mirror = target(source)
        mutations = [lambda r: r.update(owner="in-flight"), lambda r: r.update(errors=["unresolved"]),
                     lambda r: r.pop("owner"), lambda r: r["records"][0].pop("error"),
                     lambda r: r["records"][0].update(source_recurring_event_id="changed")]
        for mutate in mutations:
            registry = legacy_registry(source, mirror)
            mutate(registry)
            raw = raw_input([source], [mirror])
            use_legacy(raw, registry)
            self.blocked(raw)

    def test_incomplete_or_uncertain_planner_state_is_not_accepted(self):
        for field, value in (("status", "uncertain"), ("mappings_complete", False)):
            raw = raw_input()
            raw["state"]["value"][field] = value
            self.blocked(raw, "supplied_planner_state_not_verified_and_complete")

    def test_personal_calendar_wrong_namespace_and_wrong_window_rejected(self):
        raw = raw_input()
        raw["calendars"][0]["calendar_id"] = "personal@example.invalid"
        self.blocked(raw, "calendar_not_allowed_or_duplicate")
        raw = raw_input()
        raw["namespace"] = "a" * 32
        self.blocked(raw, "invalid_raw_envelope")
        raw = raw_input()
        raw["calendars"][0]["listing"]["time_min"] = "2026-10-06T00:00:00Z"
        self.blocked(raw, "listing_window_must_match_actual_run_start_plus_90_days")

    def test_recurrence_master_and_malformed_original_are_not_accepted(self):
        source = detail()
        source["recurrence"] = ["RRULE:FREQ=DAILY"]
        with self.assertRaisesRegex(a.AdapterError, "recurrence_not_expanded"):
            a.normalize_event(CONFIG, A, source)
        source = detail()
        source["original_start_time"] = {"dateTime": source["start"]}
        with self.assertRaisesRegex(a.AdapterError, "invalid_original_occurrence_identity"):
            a.normalize_event(CONFIG, A, source)

    def test_raw_data_and_verified_state_are_not_mutated(self):
        source = detail()
        source["attendees"] = [{"is_self": None, "response_status": "accepted", "email": "z@example.invalid"},
                               {"is_self": False, "response_status": "tentative", "email": "a@example.invalid"}]
        raw = raw_input([source])
        before = deepcopy(raw)
        first = a.adapt(raw)
        self.assertEqual(raw, before)
        self.assertEqual(a.adapt(raw), first)
        raw["calendars"].reverse()
        self.assertEqual(a.adapt(raw), first)

    def test_unmodified_pinned_planner_is_enforced(self):
        with patch.object(a, "PINNED_PLANNER_SHA256", "0" * 64):
            self.blocked(raw_input(), "pinned_planner_bytes_mismatch")

    def test_generic_cli_files_and_stdin(self):
        source = detail()
        raw = raw_input([source])
        add_marker_lookup(raw, source)
        with tempfile.TemporaryDirectory() as directory:
            input_path, output_path = Path(directory) / "arbitrary-name.json", Path(directory) / "result.json"
            input_path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(a.main([str(input_path), "-o", str(output_path)]), 0)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), a.adapt(raw))
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(raw))), patch("sys.stdout", output):
            self.assertEqual(a.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["schema_version"], p.SCHEMA_VERSION)

    def test_cli_invalid_duplicate_or_nonfinite_json_produces_only_blocked_output(self):
        for text in ("not-json", '{"x":1,"x":2}', '{"x":NaN}'):
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO(text)), patch("sys.stdout", output):
                self.assertEqual(a.main([]), 2)
            self.assertEqual(json.loads(output.getvalue())["adapter_status"], "blocked")
            self.assertIsNone(json.loads(output.getvalue())["planner_input"])

    def test_cli_partial_batch_never_emits_a_partial_planner_input(self):
        raw = raw_input([detail()])
        raw["calendars"][0]["detail_batches"][0]["responses"] = []
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(raw))), patch("sys.stdout", output):
            self.assertEqual(a.main([]), 2)
        self.assertEqual(json.loads(output.getvalue())["reason"], "requested_detail_ids_not_returned")

    def test_279_pairs_in_verified_and_explicit_legacy_modes_with_ten_id_batches(self):
        events, stored, legacy_rows = {A: [], B: []}, [], []
        base_registry = None
        for index in range(279):
            cid, other = (A, B) if index % 2 else (B, A)
            start = (p.instant("2026-10-07T09:00:00Z") + timedelta(hours=index)).isoformat()
            end = (p.instant(start) + timedelta(minutes=30)).isoformat()
            source = detail("source-" + str(index), start, end)
            if index < 277:
                source.update(original_start_time=start, recurring_event_id="synthetic-series")
            mirror = target(source, cid, "destination-" + str(index))
            events[cid].append(source)
            events[other].append(mirror)
            stored.append(mapping(source, mirror, cid))
            one_registry = legacy_registry(source, mirror, cid)
            base_registry = base_registry or deepcopy(one_registry)
            legacy_rows.extend(one_registry["records"])
        raw = raw_input(events[A], events[B], stored)
        self.assertTrue(all(len(b["requested_ids"]) <= 10 for c in raw["calendars"] for b in c["detail_batches"]))
        normalized = a.adapt(raw)
        result = p.plan(normalized)
        self.assertEqual(result["counts"], {"create": 0, "update": 0, "delete": 0, "noop": 279, "conflict": 0})
        self.assertEqual({x["marker"] for x in result["actions"]}, {x["marker"] for x in stored})
        base_registry["records"] = legacy_rows
        use_legacy(raw, base_registry)
        boot = p.plan(a.adapt(raw))
        self.assertEqual(boot["status"], "bootstrap_ready")
        self.assertEqual(boot["counts"], result["counts"])


def add_managed_lookup(data, source, source_calendar=A, namespace_candidates=None, key_candidates=None):
    import test_planner as fixtures
    destination = B if source_calendar == A else A
    marker = p.marker_for(CONFIG, source_calendar, source["id"], source["original_start_time"], destination)
    row = next(c for c in data["calendars"] if c["calendar_id"] == destination)
    row["marker_searches"].append({"marker": marker, "scope": p.MANAGED_SCOPE, "complete": True,
        "queries": [{"kind": kind, "q": query, "time_min": None, "time_max": None, "complete": True, "pages": pages(candidates)}
                    for kind, query, candidates in (("namespace", NAMESPACE, namespace_candidates or []),
                                                     ("key", marker[-65:-1], key_candidates or []))]})
    if "managed_context" not in data:
        normalized_state = deepcopy(data["state"]["value"])
        synthetic = fixtures.batch()
        synthetic["state"] = normalized_state
        data["managed_context"] = fixtures.managed(synthetic)["managed_context"]


class ManagedAdapterTests(unittest.TestCase):
    def setup_raw(self):
        source = detail()
        raw = raw_input([source])
        add_managed_lookup(raw, source)
        return raw

    def test_managed_query_pages_enable_new_create_without_universal_claim(self):
        raw = self.setup_raw()
        normalized = a.adapt(raw)
        self.assertEqual(p.plan(normalized)["counts"]["create"], 1)
        search = normalized["calendars"][1]["marker_searches"][0]
        self.assertEqual(search["scope"], p.MANAGED_SCOPE)
        self.assertNotIn("coverage_verified", search)

    def test_duplicate_candidates_between_namespace_and_key_query_are_inspected_once(self):
        source = detail()
        mirror = target(source)
        raw = raw_input([source], extra_b=[found(mirror)])
        add_managed_lookup(raw, source, namespace_candidates=[mirror], key_candidates=[mirror])
        normalized = a.adapt(raw)
        self.assertEqual(len(normalized["calendars"][1]["marker_searches"][0]["events"]), 1)
        self.assertEqual(p.plan(normalized)["counts"]["create"], 0)

    def test_query_candidate_with_ascii_spaced_marker_blocks_create(self):
        source = detail()
        mirror = target(source)
        mirror["description"] = " \t\r".join(mirror["description"])
        raw = raw_input([source], extra_b=[found(mirror)])
        add_managed_lookup(raw, source, namespace_candidates=[mirror])
        self.assertEqual(p.plan(a.adapt(raw))["counts"]["create"], 0)

    def test_unknown_extra_text_candidate_never_adopted(self):
        source = detail()
        mirror = target(source)
        mirror["description"] += " manual note"
        raw = raw_input([source], extra_b=[found(mirror)])
        add_managed_lookup(raw, source, key_candidates=[mirror])
        self.assertEqual(p.plan(a.adapt(raw))["counts"]["create"], 0)

    def test_two_different_ids_same_marker_blocks(self):
        source = detail()
        first, second = target(source, event_id="one"), target(source, event_id="two")
        raw = raw_input([source], extra_b=[found(first), found(second)])
        add_managed_lookup(raw, source, namespace_candidates=[first], key_candidates=[second])
        result = p.plan(a.adapt(raw))
        self.assertEqual(result["counts"]["create"], 0)
        self.assertIn("duplicate_marker", [x["reason"] for x in result["actions"]])

    def test_empty_pages_unread_pages_bounded_queries_and_wrong_terms_rejected(self):
        mutations = [("pages", []), ("complete", False), ("time_min", RUN), ("time_max", END), ("q", "wrong")]
        for key, value in mutations:
            with self.subTest(key=key):
                raw = self.setup_raw()
                raw["calendars"][1]["marker_searches"][0]["queries"][0][key] = value
                with self.assertRaises(a.AdapterError):
                    a.adapt(raw)
        raw = self.setup_raw()
        raw["calendars"][1]["marker_searches"][0]["queries"][0]["pages"][0]["response"]["next_page_token"] = "unread"
        with self.assertRaises(a.AdapterError):
            a.adapt(raw)

    def test_query_candidates_require_complete_actual_detail_batches(self):
        source = detail()
        raw = raw_input([source])
        add_managed_lookup(raw, source, namespace_candidates=[target(source)])
        with self.assertRaises(a.AdapterError):
            a.adapt(raw)

    def test_unbounded_queries_must_include_both_terms(self):
        raw = self.setup_raw()
        raw["calendars"][1]["marker_searches"][0]["queries"].pop()
        with self.assertRaises(a.AdapterError):
            a.adapt(raw)

    def test_missing_ledger_is_not_inferred_from_empty_search(self):
        raw = self.setup_raw()
        del raw["managed_context"]
        self.assertEqual(p.plan(a.adapt(raw))["counts"]["create"], 0)

    def test_tracked_mirror_read_outside_window_blocks_if_marker_was_erased(self):
        source, fresh_source = detail("known"), detail("new")
        mirror = target(source)
        stored = mapping(source, mirror)
        mirror["description"] = "erased"
        raw = raw_input([source, fresh_source], mappings=[stored], extra_b=[found(mirror)])
        add_managed_lookup(raw, fresh_source)
        result = p.plan(a.adapt(raw))
        self.assertEqual(result["counts"]["create"], 0)
        self.assertIn("tracked_destination_missing_or_manually_changed", [x["reason"] for x in result["actions"]])

    def test_raw_context_preserved_without_mutating_input(self):
        raw = self.setup_raw()
        original = deepcopy(raw)
        normalized = a.adapt(raw)
        self.assertEqual(normalized["managed_context"], raw["managed_context"])
        self.assertEqual(original, raw)

    def test_managed_raw_to_plan_to_guard(self):
        import test_planner as fixtures
        action = writes(p.plan(a.adapt(self.setup_raw())))[0]
        self.assertTrue(p.revalidate_action(action, fixtures.claimed_fresh(action))["allowed"])

    def test_serialized_production_raw_to_create_guard_without_atomic_storage(self):
        import test_planner as fixtures
        raw = self.setup_raw()
        raw["managed_context"] = fixtures.serialized({"managed_context": raw["managed_context"]})["managed_context"]
        action = writes(p.plan(a.adapt(raw)))[0]
        fresh = fixtures.claimed_fresh(action)
        self.assertNotIn("atomic_once_only_claim", fresh["creation_claim"])
        self.assertFalse(fresh["connector_capabilities"]["conditional_writes"])
        self.assertTrue(p.revalidate_action(action, fresh)["allowed"])


class AdapterConfigTests(unittest.TestCase):
    def configured_raw(self, cfg):
        source = detail("portable-native")
        cfg = p.validate_config(cfg)
        first, second = p.calendar_ids(cfg)
        raw = raw_input([source])
        raw["config"] = cfg
        raw["state"]["value"]["config_fingerprint"] = p.config_fingerprint(cfg)
        raw["calendars"][0]["calendar_id"] = first
        raw["calendars"][1]["calendar_id"] = second
        raw["calendars"][1]["marker_searches"] = [{"marker": p.marker_for(cfg, first, source["id"], None, second),
            "scope": p.UNIVERSAL_SCOPE, "complete": True, "coverage_verified": True, "evidence": "synthetic-full-coverage", "pages": pages([])}]
        return raw

    def test_same_adapter_bytes_work_for_distinct_calendar_and_namespace_configs(self):
        from test_planner import RuntimeConfigTests
        for cfg in (CONFIG, RuntimeConfigTests().other_config()):
            normalized = a.adapt(self.configured_raw(cfg))
            self.assertEqual(normalized["config"], p.validate_config(cfg))
            self.assertEqual(p.plan(normalized)["counts"]["create"], 1)

    def test_raw_config_required_and_no_implicit_default(self):
        raw = self.configured_raw(CONFIG)
        del raw["config"]
        with self.assertRaises(a.AdapterError):
            a.adapt(raw)

    def test_cross_config_supplied_state_is_rejected(self):
        from test_planner import RuntimeConfigTests
        raw = self.configured_raw(RuntimeConfigTests().other_config())
        raw["state"] = self.configured_raw(CONFIG)["state"]
        with self.assertRaisesRegex(a.AdapterError, "state_config_mismatch"):
            a.adapt(raw)

    def test_old_verified_state_without_binding_cannot_be_guessed(self):
        raw = self.configured_raw(CONFIG)
        del raw["state"]["value"]["config_fingerprint"]
        with self.assertRaises(a.AdapterError):
            a.adapt(raw)

    def test_legacy_bootstrap_checks_explicit_config_before_binding(self):
        source = detail()
        dest = target(source)
        raw = raw_input([source], [dest])
        legacy = legacy_registry(source, dest)
        use_legacy(raw, legacy)
        normalized = a.adapt(raw)
        self.assertEqual(normalized["state"]["config_fingerprint"], p.config_fingerprint(CONFIG))
        self.assertEqual(p.plan(normalized)["status"], "bootstrap_ready")
        legacy["namespace"] = "f" * 32
        use_legacy(raw, legacy)
        with self.assertRaisesRegex(a.AdapterError, "legacy_namespace_mismatch"):
            a.adapt(raw)

    def test_plugin_self_context_must_match_configured_verified_identity(self):
        raw = self.configured_raw(CONFIG)
        candidate = raw["calendars"][0]["detail_batches"][0]["responses"][0]["event"]
        candidate["attendees"] = [{"is_self": True, "response_status": "declined", "email": "not-self@example.invalid"}]
        with self.assertRaisesRegex(a.AdapterError, "self_attendee_not_in_verified_config"):
            a.adapt(raw)

    def test_source_content_cannot_supply_new_config_accounts(self):
        raw = self.configured_raw(CONFIG)
        raw["calendars"][0]["detail_batches"][0]["responses"][0]["event"]["config"] = {"calendar_id": "third@example.invalid"}
        normalized = a.adapt(raw)
        action = writes(p.plan(normalized))[0]
        self.assertEqual(action["destination"]["calendar_id"], B)
        self.assertEqual(p.calendar_ids(normalized["config"]), (A, B))


class RecoveryAdapterTests(unittest.TestCase):
    def raw_recovery(self):
        import test_planner as fixtures
        source = detail("synthetic-recovery-source")
        raw = raw_input([source])
        add_managed_lookup(raw, source)
        raw["managed_context"] = fixtures.serialized({"managed_context": raw["managed_context"]})["managed_context"]
        recovered = fixtures.recovery_batch(a.adapt(raw))
        raw["managed_context"] = recovered["managed_context"]
        return raw, recovered

    def test_raw_adapter_preserves_certified_recovery_and_guard_round_trip(self):
        import test_planner as fixtures
        raw, expected = self.raw_recovery()
        normalized = a.adapt(raw)
        self.assertEqual(normalized, expected)
        action = writes(p.plan(normalized))[0]
        self.assertTrue(p.preflight_action(action, fixtures.preflight_fresh(action))["ready_to_prepare"])
        self.assertTrue(p.revalidate_action(action, fixtures.claimed_fresh(action))["allowed"])

    def test_adapter_never_refreshes_stale_recovery_observations_fingerprint(self):
        raw, expected = self.raw_recovery()
        raw["state"]["value"]["generation"] = "synthetic-changed-state"
        normalized = a.adapt(raw)
        self.assertEqual(normalized["managed_context"], expected["managed_context"])
        self.assertEqual(writes(p.plan(normalized)), [])

    def raw_supersession(self):
        import test_planner as fixtures
        raw, normalized = self.raw_recovery()
        refreshed = fixtures.supersede_unused(normalized)
        raw["managed_context"] = deepcopy(refreshed["managed_context"])
        return raw, refreshed

    def test_compact_unused_supersession_survives_adapter_and_both_guards(self):
        import test_planner as fixtures
        raw, expected = self.raw_supersession()
        normalized = a.adapt(raw)
        self.assertEqual(normalized, expected)
        self.assertEqual(normalized["managed_context"]["ledger"]["recoveries"], raw["managed_context"]["ledger"]["recoveries"])
        self.assertNotIn("prior_action", normalized["managed_context"]["ledger"]["recoveries"][-1])
        action = writes(p.plan(normalized))[0]
        self.assertTrue(p.preflight_action(action, fixtures.preflight_fresh(action))["ready_to_prepare"])
        self.assertTrue(p.revalidate_action(action, fixtures.claimed_fresh(action))["allowed"])

    def test_adapter_does_not_synthesize_missing_supersession_audit(self):
        raw, _ = self.raw_supersession()
        del raw["managed_context"]["ledger"]["recoveries"][-1]["supersession"]["audit_reference"]
        with self.assertRaisesRegex(a.AdapterError, "invalid_recovery_supersession"):
            a.adapt(raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
