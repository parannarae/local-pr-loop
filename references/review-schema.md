# Review JSON Schema

The canonical file starts with:

```json
{
  "schema_version": 1,
  "review_id": "k7m3q9wx",
  "name": "feature-review",
  "state": {
    "marker": "AWAITING REVIEW",
    "latest_iteration": 0,
    "latest_event_kind": null,
    "updated_at": null
  },
  "history": []
}
```

Do not edit `state` directly. `review-json.sh publish` appends one event and
derives `state` from it.

## Event Types

Create the temporary event for a review with:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" template \
  REPO REVIEW_ID TOKEN KIND ITERATION
```

Supported kinds:

- `review`: reviewer findings, source snapshot, and validation;
- `review_correction`: affected findings, source snapshot and drift, plus newly
  assigned findings;
- `owner_response`: starting/completed snapshots, drift assessment, one
  disposition per finding, changed files, guide synchronization, and validation;
- `final_review`: reviewed-through iteration, approved snapshot, resolutions,
  validation, and decision;
- `reviewer_timeout`: owner records a missing reviewer transition;
- `owner_timeout`: reviewer records a missing complete response.

Event snapshots contain the identity fields needed to match guarded source:

```json
{
  "revision": "0000000000000000000000000000000000000000",
  "scope": ["path/to/implementation", "path/to/tests"],
  "fingerprint": "0000000000000000000000000000000000000000000000000000000000000000",
  "exclusions": [],
  "additional_inputs": [
    {
      "path": "path/to/ignored-generated-config",
      "kind": "file",
      "mode": "0600",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
    }
  ]
}
```

For a symlink additional input, `sha256` covers the resolved file content and
`link_target` records the link text. Publication detects changes to either.

Validation has this shape:

```json
{
  "performed": ["command and result"],
  "unavailable": ["check and reason"],
  "remaining_gaps": []
}
```

Finding IDs must be globally unique and match their iteration, for example
`I2-F1`. Within each iteration, `F` numbers must be positive and gap-free. A
`deferred/blocked` disposition also requires `blocker`, `completed_work`,
`remaining_work`, and `validation_gap`.

Every event uses its kind-specific timezone-aware ISO 8601 timestamp:
`submitted_at` for reviews and corrections, `completed_at` for responses and
final reviews, and `timed_out_at` for timeouts. A timeout also records
`started_at` and `deadline`. The deadline is exactly 30 minutes after an
owner response for `reviewer_timeout`, or two hours after the latest review or
correction for `owner_timeout`; premature timeouts are invalid.

## Latest Markdown Report

Generate `.latest.md` from only `.history[-1]`. Include event metadata,
validation, and the kind-specific findings, dispositions, correction, final
decision, or timeout clock. Exclude earlier discussion; canonical JSON retains
the full audit history. While holding the lock, regenerate a stale report with:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" report REPO REVIEW_ID TOKEN
```

`report` retains the lock. Release it explicitly if no further mutation follows.
