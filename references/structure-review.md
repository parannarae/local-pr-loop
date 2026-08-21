# Structure Round Review Charge

Read this before the first event of any loop whose `review_kind` is
`structure`. The charge below replaces the ordinary correctness charge for
this loop only; every loop mechanic — threads, locks, guards, evidence,
publication — works exactly as in [review-schema.md](review-schema.md).

## What This Round Is

A structure round reviews the *shape* of its whole guarded scope while
preserving behavior. It exists because correctness loops fix findings at
flagged sites and verify fixes partly by diff locality, so requirements
accumulate into whatever shape the first draft had; this round is the
amortization step. Its LGTM asserts "same behavior, better shape", never
"defects fixed".

## Reviewer Charge

Read the guarded scope whole before opening threads. A finding names a shape
across sites, not a line:

- one body mixing altitudes (orchestration interleaved with low-level detail);
- the same values threaded through many signatures instead of owning a type
  or object whose lifetime matches them;
- per-call or per-cycle state faked onto a long-lived object;
- duplicated flow that should share one implementation with explicit variation
  points, or one helper forcing unrelated flows together;
- ordering constraints that exist only as comment lore because the structure
  cannot express them;
- a mutable flag or accumulator whose meaning the reader must track across a
  long span.

Each finding is a durable thread. Set `paths` to every file the shape spans,
choose priority as usual — most shape findings are `P2` or `P3`; reserve `P1`
for a shape actively causing defects — and state `required_behavior` as the
target shape, not a site edit.

Do not raise correctness findings in this round. A defect noticed mid-round is
recorded with `add-note` on the most related thread and routed to a new
correctness loop after this one terminates; mixing the two verification
contracts in one loop makes it ambiguous which check governs a thread.

## Owner Charge

Restructure across the whole guarded scope; cross-site diffs are the expected
result. Preserve every behavior, comment rationale, and test contract. Do not
widen scope beyond the guarded set, and do not fix defects found on the way —
flag each with `add-note` on your reply and route it to a follow-up
correctness loop instead.

## Verification Contract

Behavior preservation replaces diff locality. Record with `add-check`:

- the full test suite passes on the restructured tree; and
- test files inside the guarded scope are unchanged — the strongest cheap
  evidence that only shape moved.

When a restructuring legitimately must touch test files (helper moves,
renames), the owner flags it with `add-note` and the reviewer verifies
assertion equivalence explicitly before resolving; zero test edits stays the
default expectation. Resolve a thread only after reading the restructured code
whole, not from the diff alone.
