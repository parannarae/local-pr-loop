---
name: local-pr-loop
description: Run owner or reviewer role in a repository-local JSON PR loop with durable conversation threads, immutable history, source-drift guards, validated routing, timeouts, and latest-event Markdown reports. Use for review exchanges without hosted PR comments that continue until every thread is resolved and the current source reaches LGTM.
license: MIT
metadata:
  version: "0.2.0"
---

# Local PR Loop

Store each loop under the target repository's `.local/reviews/`. `init` returns
a random `REVIEW_ID`. Each loop uses:

- `REVIEW_ID.json`: canonical state and immutable event history;
- `REVIEW_ID.latest.md`: generated report for the latest event; and
- `REVIEW_ID.event.json`: temporary event created by `template` and removed by
  `publish`.

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

Route strictly on `.state.marker`:

| Marker | Next action |
| --- | --- |
| `AWAITING REVIEW` | Reviewer: publish `review` or an initial `final_review`. |
| `OWNER ACTION REQUIRED` | Owner: reply to every open thread. |
| `REVIEWER ACTION REQUIRED` | Reviewer: verify source and decide every open thread. |
| `LGTM` or either timeout | Stop. |

Wait unless the assigned role owns the turn. A reviewer may publish
`source_update` during either active marker when the reviewed source basis must
change.

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
8. Check the latest report. Release a retained lock after failures, and reinspect
   every five minutes while following the recorded timeout clock.

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
- Include relevant ignored or generated source inputs with `--additional-input`.
- Never run loops with overlapping guarded scopes concurrently in one worktree.
- Preserve unrelated changes; lock before changing state, event, or source.
- Never publish stale hashes, claim unperformed validation, expose secrets, or
  continue after a terminal marker.
- On ambiguity, another agent's lock, or material unexplained drift, stop rather
  than guessing or repairing canonical JSON manually.

## Legacy Reviews

Preserve existing Markdown or YAML reviews verbatim. Do not translate legacy
history or start a competing overlapping loop without user authorization.
