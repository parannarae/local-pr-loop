# Review JSON Schema v2

Schema v2 models findings as durable conversation threads. The owner replies;
the reviewer alone resolves or reopens. Source updates are independent events.

## Canonical Document

```json
{
  "schema_version": 2,
  "review_id": "k7m3q9wx",
  "name": "feature-review",
  "state": {
    "marker": "AWAITING REVIEW",
    "latest_event_kind": null,
    "updated_at": null,
    "current_source_fingerprint": null,
    "open_threads": [],
    "resolved_threads": []
  },
  "history": []
}
```

Never edit `state` or `history` directly. `publish` appends one validated event
and derives the complete state projection.

## Thread Contract

New threads use globally unique, gap-free IDs: `T1`, `T2`, and so on.

```json
{
  "id": "T1",
  "priority": "P1",
  "title": "Reject malformed input",
  "risk": "Malformed input reaches persistence.",
  "evidence": "The handler passes the value without validation.",
  "required_behavior": "Validate before calling the repository."
}
```

Thread IDs remain stable for the entire conversation. Owner replies, reviewer
decisions, resolutions, and reopenings reference `thread_id`.

## Event Types

- `review`: reviewer records the initial guarded snapshot, validation, and one or
  more newly opened threads; routes to the owner.
- `source_update`: reviewer replaces the guarded snapshot and explains why.
  `thread_impacts` may be empty, comment on open threads, or reopen resolved
  threads; `new_threads` may add sequential findings discovered in the new
  source; routes to the owner.
- `owner_reply`: owner supplies starting and completed snapshots, one reply for
  every open thread, changed files, guide synchronization, and validation;
  routes to the reviewer.
- `reviewer_update`: reviewer decides every open thread using `resolve` or
  `comment`, may `reopen` resolved threads, and may open sequential new threads;
  at least one thread must remain open, then routing returns to the owner.
- `final_review`: reviewer resolves every remaining open thread and approves the
  current snapshot. With empty history, it may approve with no resolutions.
- `owner_timeout`: reviewer records a missing owner reply two hours after the
  latest reviewer event routing to the owner.
- `reviewer_timeout`: owner records a missing reviewer decision 30 minutes after
  the latest owner reply.

### Owner Reply

Every open thread appears exactly once:

```json
{
  "thread_id": "T1",
  "decision": "applied",
  "message": "Added validation before persistence.",
  "evidence": "The focused validation test passes."
}
```

`decision` is `applied`, `declined`, or `deferred/blocked`. A blocked reply also
requires `blocker`, `completed_work`, `remaining_work`, and `validation_gap`.
Owner replies never change thread resolution state.

### Reviewer Decision

`reviewer_update.decisions` addresses every currently open thread exactly once:

```json
{
  "thread_id": "T1",
  "action": "resolve",
  "message": "Verified through the public behavior test."
}
```

Use `resolve` or `comment` for open threads. Use `reopen` only for an already
resolved thread. When every open thread can be resolved, publish `final_review`
instead of `reviewer_update`.

### Source Update

The reviewer may update source during either active routing state:

```json
{
  "kind": "source_update",
  "status": "OWNER ACTION REQUIRED",
  "role": "Reviewer",
  "submitted_at": "2026-07-24T03:00:00+00:00",
  "source_snapshot": {},
  "reason": "Added an omitted changed configuration file.",
  "thread_impacts": [],
  "new_threads": [],
  "validation": {
    "performed": [],
    "unavailable": [],
    "remaining_gaps": []
  }
}
```

An empty `thread_impacts` list is correct when the source basis changes without
altering any conversation. A `reopen` impact must reference a resolved thread; a
`comment` impact must reference an open thread. Use `new_threads` for findings
discovered while inspecting the replacement source; IDs continue the global
gap-free `T<N>` sequence.

## Source Snapshots and Validation

Every guarded snapshot contains `revision`, `scope`, `fingerprint`,
`exclusions`, and `additional_inputs`. Additional inputs record path, kind,
mode, digest, and symlink target when applicable.

Validation has this shape:

```json
{
  "performed": ["command and result"],
  "unavailable": ["check and reason"],
  "remaining_gaps": []
}
```

Never claim checks that were not run.

## State and Approval Invariants

- `OWNER ACTION REQUIRED` always has at least one open thread.
- `owner_reply` addresses every open thread but leaves them open.
- `REVIEWER ACTION REQUIRED` follows a complete owner reply.
- `source_update` may preserve, reopen, or add threads and routes to the owner.
- `reviewer_update` addresses every open thread and leaves at least one open.
- `final_review` resolves every remaining open thread against the current source.
- `LGTM` has no open threads.
- No event may follow LGTM or a timeout.

## Latest Markdown Report

Generate `.latest.md` from `.history[-1]` plus the derived open/resolved thread
lists. Include event metadata, source identity, event-specific messages, and
validation. Canonical JSON retains the complete conversation history.

While holding the lock, regenerate a stale report with:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" report REPO REVIEW_ID TOKEN
```

`report` retains the lock. Release it explicitly if no mutation follows.
