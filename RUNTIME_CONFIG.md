# Explicit runtime configuration and distribution boundary

Planner 1.3.2 requires input schema 3. Adapter 1.2.2 requires raw schema 2.
RuntimeConfig version is 1. Managed ledger version is 2. Old input shapes are
rejected; no default accounts, implicit namespace or automatic state rebinding.

Both raw adapter input and normalized planner input require the same `config`
object. It REPLACES the former top-level input `namespace` field. The planner's
output still reports the configured namespace plus `config_fingerprint`.

## Exact configuration schema

```json
{
  "version": 1,
  "namespace": "0123456789abcdef0123456789abcdef",
  "calendars": [
    {
      "calendar_id": "calendar-alpha@example.invalid",
      "self_identities": ["calendar-alpha@example.invalid"],
      "self_identities_verified": true
    },
    {
      "calendar_id": "calendar-beta@example.invalid",
      "self_identities": ["calendar-beta@example.invalid"],
      "self_identities_verified": true
    }
  ]
}
```

These are reserved synthetic example identities, not deployment defaults. The
executable contains none of them. `config.example.json` is runnable synthetic
input; `config.template.json` deliberately fails validation until its placeholders
are filled and identity verification has actually occurred. Neither file is
loaded implicitly. Merge a private validated config into the input envelope.

Every shown key is mandatory; unknown config/row keys are rejected.

- version: integer 1, not boolean.
- namespace: exactly 32 lowercase hexadecimal characters. Preserve an existing
  deployment's namespace bytes when keeping its issued markers.
- calendars: exactly TWO entries with distinct exact calendar_id strings. They
  define the entire bidirectional allowlist; each source can target only the other.
- calendar_id: nonempty explicit ID, without whitespace/control characters or
  leading/trailing spaces. `primary`, `default`, `personal` shortcuts in any case
  are rejected. Actual IDs remain byte-for-byte unchanged, including case and
  non-ASCII characters; do not replace raw IDs with aliases or a discovered account.
- self_identities: nonempty list of caller-verified email identities for this
  calendar's authenticated account context. No guesses from calendar_id, organizer,
  source text or default connection. Case-insensitive duplicates are rejected.
- self_identities_verified: literal true, certifying the caller actually verified
  those identities through its existing authenticated integration. The offline
  planner cannot independently authenticate this certificate.

The same identity may legitimately occur in both calendar entries if independently
verified for both. No additional calendar is authorized by adding an email identity.
No event/description/attendee content can add a calendar, namespace or identity.
No Space, project, thread, Library or account-link IDs belong in this schema.
The external coordinator selects its actual authenticated connection separately.

## Canonical binding and self-response verification

`validate_config(config)` returns a fresh canonical copy: calendar rows sort by
their unchanged calendar_id; self identities casefold and sort. Object keys use
the existing canonical JSON serializer. `config_fingerprint(config)` is SHA-256 of
that canonical UTF-8 compact sorted-key JSON. Reordering calendars/identities or
changing only email letter case does not change the fingerprint. Calendar ID and
namespace bytes DO affect it. No module-global deployment config is mutated;
distinct configurations can be used concurrently in the same Python process.

The existing connector `is_self` flag and response_status remain authoritative
observations, never inferred from an email or invented from source content.
An is_self=true attendee must expose an email present in that calendar's verified
identity list. Missing/unrecognized email is a conflict. A configured self email
with a false/null is_self flag is contradictory and fails closed rather than
guessing attendance or ignoring a possible decline. Guest false/null flags keep
their existing normalization. No self attendee still means self_response=none.
All other eligibility, time, marker and protected-field policies remain unchanged.

## State, ledger, action and reread contracts

State has one additional required field:

```text
state.config_fingerprint = config_fingerprint(config)
```

This applies to verified, bootstrap, absent and uncertain state. It does not make
absent/uncertain state verified; their existing mutation blocks remain.
Every mapping's source/destination IDs and recomputed marker must also match the
supplied config. Missing or mismatched binding rejects the entire input.

ManagedContext has the same writer/ledger structure except:

```text
managed_context.ledger.version = 2
managed_context.ledger.config_fingerprint = config_fingerprint(config)
```

Even an empty ledger must be explicitly bound. Completeness, durable history,
provenance, known-ID reads and serialized/optional atomic execution requirements
remain mandatory. Never silently reset or relabel a ledger to fit a new config.

Every action includes expected.config, expected.state_config_fingerprint, and the
canonical config fingerprint in expected.fingerprints.config. Those fields enter
the action integrity hash. Fresh pre-write input additionally requires:

```text
fresh.schema_version = 3
fresh.config = latest explicit runtime config
fresh.state_config_fingerprint = binding from the latest loaded persisted state
```

All existing fresh fields remain required. The guard validates config, compares
the state binding and expected fingerprints, and checks every observed calendar's
scope. Managed durable-intent transitions retain the same ledger binding. A plan
or creation record from another config cannot be replayed by merely changing
generation labels or supplying a new config. This is data separation, not a
cryptographic authorization mechanism: the caller must protect its private config
and journal and must never deliberately falsify snapshots/certificates.

## Explicit migration without calendar changes

The coordinator handles private migration under the existing serialized pause/
drain admission. This package performs no live state persistence or cutover.

1. Load the actual complete verified state and durable history; hold unresolved
   outcomes for reconciliation. Supply the original namespace and exact raw
   calendar IDs through private RuntimeConfig. Verify self identities.
2. Check every mapping/issued marker against those same configured IDs and
   namespace. Preserve source occurrence IDs, original raw start string/null,
   destination IDs, marker strings, baseline fields, provenance and operation
   history. Do not reconstruct identities from a hash or current start/end.
3. Explicitly add state.config_fingerprint. For an already verified older ledger,
   explicitly set version=2 and add the same config_fingerprint after the complete
   audit. Persist new generations/readback through the existing coordinator.
   Do not label unknown state/history as complete or infer a blank ledger.
4. Supply current certified calendar/detail reads and run the new adapter/planner.
   Compare marker strings and zero-change/noop results before cutover. Any mismatch
   stops migration. Do not replace protected baselines merely to suppress conflicts.

The explicit legacy-bootstrap adapter mode checks the legacy registry's namespace,
its exact two calendar IDs, original hash inputs and current details against the
provided config, then emits bound bootstrap input. It still requires the planner's
full bootstrap validation and external persistence; it does not auto-migrate an
unbound newer verified state or ledger.

## Unchanged marker bytes and public APIs

`marker_for(config, source_calendar_id, source_event_id, original_start_or_null,
destination_calendar_id)` hashes exactly:

```text
["dot-block-sync",1,config.namespace,source_calendar_id,source_event_id,
 source_original_start_time_or_null,destination_calendar_id]
```

Serialization remains compact JSON, ensure_ascii=false, UTF-8, with SHA-256
lowercase hex, wrapped in `[dot-block-sync:v1:<namespace>:<hash>]`. The new config
fingerprint and self identities are NOT added to this payload. Supplying unchanged
old namespace/raw IDs/original strings preserves every existing marker. Current
start/end never enter the key. Tests compare 279 synthetic occurrences in each of
two unrelated configs against the original serialization recipe, plus an
independent fixed SHA-256 vector. Live compatibility is checked by the coordinator.

Other config-taking helpers: description_kind(config, description),
pair_ref(config, ref), validate_managed_context(config, context),
validate_indexed_queries(config, search), and
validate_event(event, capabilities, config=config, calendar_id=explicit_id).
Adapter normalization is normalize_event(config, calendar_id, raw_event,
timezone_certificate=None). plan(data), adapt(raw), revalidate_action(action,fresh)
and the JSON CLI retain their entry-point forms with the new envelopes.

## Distributable contents

Package only generic source, synthetic tests/examples, config templates, contracts,
clean test reports, file inventories and hashes of this clean version. Private
configuration, state, ledgers, historical audits/readbacks, account/Space/project/
thread IDs, local user paths and older private archives are excluded. Publication
requires explicit authorization for the exact repository. Do not infer a licence,
change repository visibility/access, or publish runtime files with this package.
