# Review Artifact Format

The skill version is `0.5.0`. Persisted compatibility uses an independent
calendar revision:

```json
{
  "format": "local-pr-loop",
  "format_revision": "2026-08-15.1",
  "created_by": {"version": "0.5.0"},
  "created_at": "2026-08-15T01:00:00+00:00",
  "review_id": "k7m3q9wx",
  "prior_review_id": null,
  "name": "feature-review",
  "state": {
    "workflow": {
      "phase": "awaiting_initial_review",
      "primary_actor": "reviewer",
      "primary_action": {"kind": "publish_initial_review"},
      "allowed_events_by_actor": {
        "reviewer": ["review", "final_review"]
      }
    },
    "source_fingerprint": null,
    "threads": {"open": [], "resolved": []},
    "validation_gaps": {"open": [], "resolved": []},
    "latest_event": null,
    "terminal": null
  },
  "history": []
}
```

Canonical state is a pure projection of immutable history. Operation state,
deadlines becoming eligible, drafts, locks, reports, and publication recovery
never enter canonical history. Old revisions are preserved but rejected; create
a new loop rather than migrating them.

## Event Envelope and Routing

Every event contains a unique `event_id`, `kind`, and timezone-aware
`occurred_at`. Timestamps strictly increase. Event kind determines actor:

- Reviewer: `review`, `source_update`, `reviewer_update`, `final_review`,
  `owner_timeout`.
- Owner: `owner_reply`, `reviewer_timeout`, `initial_review_timeout`.

Valid workflow phases are `awaiting_initial_review`, `owner_response`,
`reviewer_verification`, and `terminal`. `allowed_events_by_actor` is
authoritative; `primary_actor` describes the main handoff without excluding
other allowed events.

Timeout events contain `started_at`, exact `deadline`, `reason`, and
`occurred_at`. `owner_timeout` and `reviewer_timeout` anchor on the latest
handoff event; `initial_review_timeout` anchors on the document's
`created_at`, since that handoff starts before any event exists, and lets the
owner terminate a loop whose reviewer never appeared. `inspect` computes
whether a structurally allowed timeout has become eligible. Wall-clock
eligibility never changes canonical state.

## Threads and Evidence

Thread IDs are globally gap-free `T<N>` values and remain stable:

```json
{
  "id": "T1",
  "priority": "P1",
  "contract": "external",
  "title": "Reject malformed input",
  "risk": "Malformed input reaches persistence.",
  "evidence": {
    "basis": "captured_fixture",
    "provenance": "sanitized fixture fixtures/rejection.json",
    "observed_at": "2026-07-25T03:00:00+00:00",
    "sanitized_result": "The service accepted the malformed field.",
    "artifact_digest": "lowercase-sha256-when-present"
  },
  "required_behavior": "Reject before persistence."
}
```

Evidence bases are `source_inspection`, `test_result`, `live_probe`,
`captured_fixture`, and `authoritative_contract`. External-contract P1/P2
threads require one of the last three; a synthetic counterexample is not enough.
`captured_fixture` evidence requires `artifact_digest`, and evidence observation
cannot postdate its containing event. Never record credentials, signed URLs,
query tokens, cookies, raw responses, raw headers, or unredacted commands.
Canonical documents and nested records use closed field sets; unknown fields
are rejected.

## Validation

```json
{
  "performed": [
    {
      "check": "bounded metadata probe",
      "result": "passed",
      "evidence": {
        "basis": "live_probe",
        "provenance": "resource video-14195539; ffprobe 8.0",
        "observed_at": "2026-07-25T03:00:00+00:00",
        "sanitized_result": "One video and one audio representation observed."
      }
    }
  ],
  "gaps": [
    {
      "gap_id": "G1",
      "check": "full media download",
      "reason": "Intentionally bounded to ten seconds.",
      "material": false
    }
  ]
}
```

Record only checks actually performed. Gap IDs are globally sequential and
stable. A failed check requires a matching material gap. Reviewer events resolve
gaps explicitly with `gap_resolutions`, each containing `gap_id`, `message`, and
structured evidence. LGTM is invalid while any historical material gap remains
open or any final check failed.

## Conversation Events

- `review` opens initial sequential threads and routes to the owner.
- `source_update` replaces the guarded snapshot, may comment/reopen threads or
  add sequential threads, and routes to the owner.
- `owner_reply` records starting/completed snapshots and replies exactly once to
  every open thread; it never resolves.
- `reviewer_update` comments on or resolves every open thread, may reopen
  resolved threads or add new threads, and leaves at least one thread open.
- `final_review` resolves every remaining open thread, or initially approves
  with none.
- `owner_timeout` and `reviewer_timeout` terminate their active handoff.

An owner reply decision is `applied`, `declined`, or `deferred/blocked`.
Blocked replies also require `blocker`, `completed_work`, `remaining_work`, and
`validation_gap`.

Any per-thread message may carry `Note to user:` lines — machine-written by the
`add-note` helper — flagging design, contract, or business-logic shifts or
decisions that need the user's attention. Threads themselves accept an
optional `message`, so a finding can be flagged at raise time in a `review` or
`new_threads` entry. The summary report lifts notes verbatim into its first
section; mechanical fixes are never flagged. `deferred/blocked` replies,
timeout terminals, and material gaps open at terminal surface there
automatically without a marker.

A reviewer may resolve a declined reply only with:

```json
{
  "thread_id": "T1",
  "action": "resolve",
  "message": "Verified through a different interface.",
  "verification": {
    "independent": true,
    "evidence": {
      "basis": "live_probe",
      "provenance": "sanitized bounded probe",
      "observed_at": "2026-07-25T03:05:00+00:00",
      "sanitized_result": "The declined behavior is correct."
    }
  }
}
```

If independent verification is unavailable, use `comment`, record the exact
unavailable check as a validation gap, and keep the thread open.

## Publication and Operation

`publish` performs lock, canonical SHA, source fingerprint, event, and event
snapshot checks inside one structured-result boundary. It verifies lock
ownership again immediately before the commit point and writes a durable receipt
before replacing canonical JSON. The receipt
contains event ID, draft digest, base and intended canonical SHA-256, source
fingerprint, and commit phase. Atomic canonical replacement is the sole commit
point. Report generation, draft removal, lock release, and receipt removal are
cleanup.

Every publication result includes `status`, `committed`, current
`canonical_sha256`, `event_id`, `lock_state`, and exact `recovery_action`.
`precommit_failed` means canonical history did not change. A surviving
`prepared` receipt is aborted by `recover-publish` after verifying its lock,
base canonical SHA, and draft digest; then source and canonical state must be
reinspected.
`published_cleanup_required` means it did change. After any nonzero result,
inspect canonical history first. Never retry an old SHA when the event is
already present; run `recover-publish`. Recovery identifies the publication by
event ID and receipt digest, regenerates the report, cleans the matching draft,
and never appends a duplicate event.

`inspect` derives one artifact status: `clean`, `editing_draft`,
`ready_to_publish`, `prepared_precommit`, `committed_cleanup`, `stale_report`, or
`corrupt_artifact`. Lock status is orthogonal, so a locked valid draft remains
`ready_to_publish` while its separate `lock_status` is `locked`. `clean` means
canonical validation passes, report matches canonical, and no draft or receipt
exists; the overall operation is idle only when `lock_status` is also `unlocked`.

`inspect --json` returns the same workflow model as the compact human dashboard:
expected responder, permitted events by actor, open threads and gaps, operation
health, source drift, timeout eligibility, stale-approval state, and one exact
recommended command. `threads --json` returns each durable conversation with
its original finding and all later replies and decisions.

State-aware templates prefill guarded snapshots, one required action per open
thread, open gap resolutions, independent-verification placeholders for declined
threads, and timeout clocks. Empty semantic fields remain for the responsible
actor to complete.
