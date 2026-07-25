---
name: local-pr-loop
description: Run owner or reviewer role in a repository-local JSON PR loop with durable conversation threads, immutable history, source-drift guards, validated routing, timeouts, and latest-event Markdown reports. Use for review exchanges without hosted PR comments that continue until every thread is resolved and the current source reaches LGTM.
license: MIT
metadata:
  version: "0.3.0"
---

# Local PR Loop

Store each loop under the target repository's `.local/reviews/`. `init` returns
a random `REVIEW_ID`. Each loop uses:

- `REVIEW_ID.json`: canonical state and immutable event history;
- `REVIEW_ID.latest.md`: regenerable report cache;
- `REVIEW_ID.event.json`: temporary event created by `template` and removed by
  `publish`; and
- `REVIEW_ID.publish.json`: durable publication receipt, present only while
  cleanup or recovery remains.

Keep the ID through handoffs. Never reuse another repository or worktree's
artifacts or guess an ambiguous ID. Treat only canonical JSON as state; generate,
never hand-edit, the Markdown report.

## Dependencies

Require Bash, Git, and Python 3.9 or newer; helpers use only the standard
library. Run state operations through:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" COMMAND ...
```

Before mutation, read [review-schema.md](references/review-schema.md) for event
contracts and [source-state.md](references/source-state.md) for exact commands.

The cooperative lock lives under the target worktree's Git metadata. Require
permission to write that metadata before acquiring it. If the environment blocks
the lock, request access; never bypass or relocate it.

## Conversation and Role Model

Use stable `T<N>` IDs as durable review conversations:

- `review` opens the initial threads.
- `owner_reply` replies to every open thread but never resolves one.
- `reviewer_update` resolves or comments on every open thread and may reopen
  resolved threads or open new ones.
- `source_update` replaces the guarded source independently of conversation
  status and may reopen, comment on, or add threads.
- `final_review` resolves every remaining open thread and approves the current
  source, or approves an initial review with no findings.

- **Reviewer:** Verify source and evidence. Open threads with `review`; use
  `reviewer_update` for partial progress and `source_update` for source-basis
  changes. Publish `final_review` only when all remaining threads can be resolved.
- **Owner:** Reply to every open thread. Apply justified changes, explain declined
  or blocked work, synchronize behavior guides, and record evidence.

Only the reviewer resolves or reopens threads. Preserve all replies and decisions
as immutable events and derive thread state from history.

## Starting and Scoping a Loop

Inspect `.local/reviews/` first and preserve legacy review artifacts. If no loop
exists and the user requested a review, initialize one with a concise name. For
an existing loop, use its supplied ID or uniquely identify it from canonical
state and requested scope; ask when more than one loop is plausible.

Determine the comparison base and complete guarded scope as described in
[source-state.md](references/source-state.md); ask when either is ambiguous.
After review begins, publish `source_update` before further owner work whenever
the guarded basis changes. Record only actual thread impacts, and add any
findings discovered in the replacement source as sequential new threads.

## Role and Routing

Run `inspect` and route on `.state.workflow`. Read `phase`, `primary_actor`,
`primary_action`, and `allowed_events_by_actor` together. The primary actor owns
the main handoff, but another actor may still publish an explicitly allowed
`source_update` or timeout event. Never infer exclusivity from `primary_actor`.

Treat `awaiting_initial_review`, `owner_response`, `reviewer_verification`, and
`terminal` as the only workflow phases. Treat `operation.status` as local
artifact condition, not workflow state. Operation status is derived and never
written to canonical history.

After a terminal event, treat later source changes as unreviewed. Leave terminal
history immutable, start a new review ID, and mention the prior ID in the
handoff.

## Common Procedure

1. For a new loop, run `init REPO NAME` and retain its `REVIEW_ID`.
2. Run `inspect REPO REVIEW_ID ...`; read instructions, source, context, tests,
   worktree changes, and canonical history.
3. Acquire the lock before changing declared source or creating an event.
4. Verify drift and run focused validation.
5. Create the event with `template REPO REVIEW_ID TOKEN KIND`.
6. Populate the event, run `validate-event`, then repeat `inspect` under lock.
7. Run `publish` with the final inspection's exact hashes and unchanged scope.
8. Read the structured publication result. After any nonzero result, inspect
   canonical state first. If `committed` is true or the event is latest, never
   retry the old SHA; run `recover-publish`.
9. Check the latest report. Release a retained lock only after a precommit
   failure. Reinspect every five minutes while following the recorded timeout.

Use priorities consistently:

- `P0`: immediate data-loss, security, or outage risk;
- `P1`: must fix before merge; clear correctness, durability, or contract failure;
- `P2`: should fix before merge; quality-gate failure, maintainability issue, or
  important edge case; and
- `P3`: non-blocking improvement.

## Non-Negotiable Rules

- Populate only the temporary event created by `template`. Let only `publish`
  append canonical history and derive state.
- Use globally unique, gap-free `T<N>` IDs. Reuse a thread ID for the same
  conversation across replies, resolutions, and reopenings.
- Record timezone-aware ISO 8601 timestamps. Base owner timeout on the latest
  reviewer event routing to the owner and reviewer timeout on the latest owner
  reply.
- Keep `event_id` unique and `occurred_at` strictly increasing.
- For external-contract P1/P2 findings, record `live_probe`,
  `captured_fixture`, or `authoritative_contract` evidence with provenance,
  observation time, and sanitized result. A synthetic counterexample alone is
  insufficient.
- Mark every validation gap `material: true|false`. Never publish LGTM with a
  material gap.
- Resolve an owner's declined thread only after independent reviewer
  verification. If that check is unavailable, comment with the exact unavailable
  check and leave the thread open.
- Bound network and media probes by metadata, HEAD/range, or short `ffprobe`
  checks. Record stable resource IDs, observation time, tool version, and
  sanitized fields/counts/results. Never persist signed URLs, query tokens,
  cookies, raw headers or bodies, or unredacted commands.
- Include relevant ignored or generated source inputs with `--additional-input`.
- Never run loops with overlapping guarded scopes concurrently in one worktree.
- Preserve unrelated changes; lock before changing state, event, or source.
- Never publish stale hashes, claim unperformed validation, expose secrets, or
  continue after a terminal phase.
- Treat canonical JSON as authoritative if it disagrees with a draft, receipt,
  report, terminal output, or another agent. The report is only a cache.
- Never break a lock using PID or elapsed age. Lock status deliberately omits its
  token.
- On ambiguity, another agent's lock, or material unexplained drift, stop rather
  than guessing or repairing canonical JSON manually.

## Unsupported Revisions

Preserve old artifacts verbatim but do not migrate or edit them. This
pre-1.0 skill accepts only its current calendar `format_revision`; initialize a
new review ID when validation reports an unsupported revision. Do not start a
competing overlapping loop without user authorization.
