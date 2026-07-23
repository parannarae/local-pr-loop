---
name: local-pr-loop
description: Run owner or reviewer role in a repository-local JSON PR loop with ID-isolated files, immutable history, source-drift guards, validated routing, timeouts, and a generated latest-event Markdown report. Use for review exchanges without GitHub comments that continue until LGTM.
license: MIT
metadata:
  version: "0.1.0"
---

# Local PR Loop

Store each loop under the target repository's `.local/reviews/`. `init` returns
a random `REVIEW_ID`. Each loop uses:

- `REVIEW_ID.json`: canonical state and immutable history;
- `REVIEW_ID.latest.md`: generated human-readable report for the latest event;
- `REVIEW_ID.event.json`: temporary event created by `template` and removed by
  `publish`.

Keep the ID through handoffs; never reuse another repository or worktree's
artifacts or guess an omitted ID. Treat only canonical JSON as state; generate,
never hand-edit, the Markdown report.

## Dependencies

Require Bash, Git, and Python 3.9 or newer; helpers use only the standard
library. Run state operations through:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" COMMAND ...
```

Before mutation, read [review-schema.md](references/review-schema.md) for event
contracts and [source-state.md](references/source-state.md) for exact commands.

## Role and Routing

Use the role assigned by the user and route on `.state.marker`:

| Marker | Next action |
| --- | --- |
| `AWAITING REVIEW` | Reviewer: perform the initial review. |
| `REVIEW SUBMITTED` | Owner: process the open iteration. |
| `PR COMPLETED` | Reviewer: verify the response and source. |
| `LGTM` or either timeout | Stop. |

Wait unless the assigned role owns the turn; only a reviewer correcting their
active submitted review may act out of turn.

## Common Procedure

1. For a new loop, run `init REPO NAME` and retain its generated `REVIEW_ID`.
2. Run `inspect REPO REVIEW_ID ...`, then read project instructions, source,
   callers, configuration, guides, tests, worktree changes, and review history.
3. Acquire the lock before changing declared source or creating the event.
4. Verify drift, run focused validation, then create the event with
   `template REPO REVIEW_ID TOKEN KIND ITERATION`.
5. Populate the generated event, run `validate-event`, then repeat `inspect`
   while holding the lock.
6. Run `publish` with the final inspection's exact hashes and unchanged scope.
7. Check the generated latest report. Release a retained lock after failures,
   and reinspect every five minutes while following the recorded timeout clock.

## Role Decisions

- **Reviewer:** Verify source and validation, not the summary alone. Publish
  `review` for findings, `final_review` when none remain, or
  `review_correction`—without changing its iteration—for an active review error.
- **Owner:** Evaluate every finding, apply justified changes, synchronize
  behavior guides, and publish one disposition and its evidence per finding.

## Non-Negotiable Rules

- Let only `publish` append `.history` and derive `.state`; never alter events.
- Use sequential iterations and globally unique `I<N>-F<M>` finding IDs.
- Record timezone-aware ISO 8601 timestamps and base timeouts on recorded event
  times. Reinspect under the lock immediately before publishing a timeout.
- Include relevant ignored or generated source inputs with `--additional-input`.
- Treat IDs as artifact isolation only; never run loops with overlapping source
  scopes concurrently in the same worktree.
- Preserve unrelated changes; lock before changing state, event, or source.
- Never publish stale hashes, claim unperformed validation, expose secrets, or
  continue after a terminal marker.
- On validation ambiguity, another agent's lock, or material source drift, stop
  instead of guessing or repairing canonical JSON manually.

## Legacy Reviews

Preserve an existing Markdown or YAML review verbatim. Do not start a competing
loop or translate legacy history without user authorization.
