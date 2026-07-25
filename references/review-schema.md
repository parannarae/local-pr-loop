# Review Artifact Format

The skill version is `0.3.0`. Persisted compatibility uses an independent
calendar revision:

```json
{
  "format": "local-pr-loop",
  "format_revision": "2026-07-25.1",
  "created_by": {"version": "0.3.0"},
  "review_id": "k7m3q9wx",
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
- Owner: `owner_reply`, `reviewer_timeout`.

Valid workflow phases are `awaiting_initial_review`, `owner_response`,
`reviewer_verification`, and `terminal`. `allowed_events_by_actor` is
authoritative; `primary_actor` describes the main handoff without excluding
other allowed events.

Timeout events contain `started_at`, exact `deadline`, `reason`, and
`occurred_at`. `inspect` computes whether a structurally allowed timeout has
become eligible. Wall-clock eligibility never changes canonical state.

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
Never record credentials, signed URLs, query tokens, cookies, raw responses, raw
headers, or unredacted commands.

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
      "check": "full media download",
      "reason": "Intentionally bounded to ten seconds.",
      "material": false
    }
  ]
}
```

Record only checks actually performed. LGTM is invalid while any gap is
material.

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

`publish` writes a durable receipt before replacing canonical JSON. The receipt
contains event ID, draft digest, base and intended canonical SHA-256, source
fingerprint, and commit phase. Atomic canonical replacement is the sole commit
point. Report generation, draft removal, lock release, and receipt removal are
cleanup.

Every publication result includes `status`, `committed`, current
`canonical_sha256`, `event_id`, `lock_state`, and exact `recovery_action`.
`precommit_failed` means canonical history did not change.
`published_cleanup_required` means it did change. After any nonzero result,
inspect canonical history first. Never retry an old SHA when the event is
already present; run `recover-publish`. Recovery identifies the publication by
event ID and receipt digest, regenerates the report, cleans the matching draft,
and never appends a duplicate event.

`inspect` derives one operation status: `clean`, `editing_draft`,
`publication_committed`, `cleanup_required`, or `locked`. Without a matching
token it reports `locked`, not ownership-specific assumptions. `clean` means
canonical validation passes, report matches canonical, and no draft, receipt, or
active lock exists.
