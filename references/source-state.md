# Source State and Publication

Commands assume the skill directory is `SKILL_DIR`. `REPO` may be any path
inside the target Git worktree; the helper resolves its root.

## Create an Isolated Loop

Ensure `REPO/.local/` is ignored, then initialize:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" init REPO feature-review [--base-ref BASE_REF]
```

`init` returns an eight-character random `REVIEW_ID` and creates canonical JSON
plus the latest report beneath `.local/reviews/`. Keep the ID unchanged.

## Determine the Guarded Scope

Determine the comparison base from the user's request or pull-request metadata.
Ask when more than one base is plausible. Pass the same base to
`init --base-ref BASE_REF`, which records its merge base with HEAD as the
accretion ledger's growth baseline — for uncommitted-only work pass
`--base-ref HEAD`; without a recorded base the ledger falls back to its
thread signal alone. Collect the mechanical candidate set with one command;
omit `BASE_REF` from `scope-candidates` when reviewing uncommitted work only:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" scope-candidates REPO [BASE_REF]
```

It returns the merge base plus changed paths grouped by kind (merge-base diff,
staged, unstaged, untracked) and their union. Selecting from the candidates is
judgment, not mechanics: guard relevant modified implementation, tests,
configuration, deployment manifests, and guides. Read unchanged callers and
dependencies as context without adding them to scope. Add relevant ignored or
generated files with `--additional-input`.

An ignored or generated file must be declared with `--additional-input`, never as
an ordinary scope path. A snapshot records reviewed paths in `scope` and every
ignored input in `additional_inputs` with its own content digest; an ignored file
listed as ordinary scope appears in neither the tracked diff nor the untracked
listing, so it would contribute its name to the fingerprint with no content behind
it. The declaration travels as structured data, so an option name can never be
recorded as a reviewed path, and a locked inspection and an unlocked inspection of
the same declaration compute identical scope, additional inputs, and fingerprint.
A disagreement between them is a defect, not drift.

The same path may not be declared both as reviewed scope and as an additional
input, and a scope declaration must name at least one reviewed path.

Guard creation also refuses a scope overlapping another non-terminal loop in this
worktree, comparing reviewed paths and additional inputs with a directory treated
as its whole subtree. The refusal names the blocking review, its phase, and the
overlapping paths. The check runs inside the lease-verified critical section that
creates the guard, so two first inspections cannot both find the scope free.

When a snapshot drifts, `inspect` reports which parts moved. Digested entries,
meaning untracked files and additional inputs, are named individually as added,
removed, or modified. Tracked changes are reported only as one aggregate flag,
because individual tracked paths cannot be recovered from a diff digest; the
dashboard says so rather than guessing paths. Scope and exclusion changes and a
changed base revision are reported separately.

## Inspect

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" inspect \
  REPO REVIEW_ID \
  --additional-input path/to/ignored-generated-config \
  path/to/source path/to/tests path/to/guide.md
```

`inspect` leads with a role-aware action dashboard and one exact recommended
command. Add `--json` immediately after `REVIEW_ID` for the equivalent stable
agent view. The snapshot covers scoped staged
and unstaged diffs, non-ignored untracked contents, and additional-input
metadata. A symlink digest covers its resolved regular-file content and records
its link target.

Use the same repository, scope, exclusions, and additional inputs for a
publication. If the reviewed source basis changes after review begins, the
reviewer publishes `source_update` with the replacement snapshot before owner
work continues. The event may use an empty `thread_impacts` list and may add
sequential `new_threads` discovered in the replacement source.

## Lock Before Mutation

Acquire the cooperative lock before changing canonical state, creating an event,
or changing declared source:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" lock acquire REPO REVIEW_ID
python3 "$SKILL_DIR/scripts/review_cli.py" inspect REPO REVIEW_ID SCOPE...
```

Acquisition creates a permission-restricted local lease and prints no token.
The second inspection creates an opaque guard containing the canonical and
source identities. A reviewer may analyze first, but must acquire and inspect
before `template`.

## Prepare and Validate an Event

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" template \
  REPO REVIEW_ID owner_reply

python3 "$SKILL_DIR/scripts/review_cli.py" validate-event REPO REVIEW_ID
```

The template prepopulates guarded snapshots and every role-required thread/gap
entry. Populate only its remaining blanks. Use:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" threads REPO REVIEW_ID --json
python3 "$SKILL_DIR/scripts/review_cli.py" add-check \
  REPO REVIEW_ID passed "focused tests"
python3 "$SKILL_DIR/scripts/review_cli.py" add-gap \
  REPO REVIEW_ID "live probe" "service unavailable" --material
python3 "$SKILL_DIR/scripts/review_cli.py" add-note \
  REPO REVIEW_ID T2 "bump deferred to the release commit" --tag decision
```

`add-note` appends a machine-formatted user-facing note to the draft's reply,
decision, resolution, or thread impact for the given thread, or to the thread
itself when it is being raised in this draft; use it only for design-shifting
changes, never mechanical fixes.

Every draft helper restamps the draft's `occurred_at` when it writes, so
evidence recorded after templating never postdates its event. Never hand-edit
the timestamp.

Use the event kind and fields defined in
[review-schema.md](review-schema.md).

## Guarded Publication

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" publish \
  REPO REVIEW_ID
```

`publish` returns structured results for lock, canonical/source drift, draft,
snapshot, commit, and cleanup failures. It verifies lock ownership before
preflight and again immediately before writing, validates canonical and source
identities and the event against immutable history, writes a durable receipt,
and atomically replaces canonical JSON. Canonical replacement is the sole commit point. Report
generation, draft removal, receipt removal, and tombstoned lock release are
recoverable cleanup.

Every result reports `committed`. After any nonzero result, inspect canonical
state before doing anything else. For `precommit_failed`, fix the problem and
reuse the unchanged draft or release the lock. For
`published_cleanup_required`, never retry the old canonical SHA; recover:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" recover-publish \
  REPO REVIEW_ID
```

For a prepared precommit receipt, recovery verifies the base canonical SHA and
draft digest, then aborts the preparation without publishing; retain the lock,
reinspect, and publish with fresh guards. For a committed receipt, recovery
verifies the event in canonical history, regenerates the report, removes only a
matching draft, releases the matching lock, and removes the receipt without
appending another event. If cleanup had already released the lock, recovery
detects the unlocked state without requiring a lease.

Never remove another agent's lock or infer staleness from PID or elapsed time.
Release atomically renames the active lock to a unique inactive tombstone before
best-effort cleanup. Lock status omits the token; ask the user how to proceed.

## Waiting, Timeouts, and Follow-ups

Wait without polling manually:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" wait REPO REVIEW_ID 300
```

The command returns on canonical change, the active handoff deadline, or its
bounded timeout. A `timeout` result is not an answer — only a changed
`canonical_sha256` is. The waiting actor re-arms until the status is `changed`
or it becomes eligible to publish a timeout event. Every phase carries a
handoff deadline: `awaiting_initial_review` anchors on the document's
`created_at`, the other phases on their latest handoff event.

Span a handoff with one bounded call instead of hand-rolling that re-arm loop:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" await-handoff \
  REPO REVIEW_ID --round-seconds 300 --max-rounds 24
```

`await-handoff` prints, on entry, who it is waiting for and whether a handoff
deadline exists, then re-arms `wait` per round until one structured outcome:

| Status | Exit | Required next step |
|---|---|---|
| `changed` | 0 | re-`inspect` and act on the new phase |
| `terminal` | 0 | run the terminal `inspect` and check `approval_stale` |
| `timeout_eligible` | 4 | run `publish-timeout --if-eligible` |
| `exhausted` | 5 | report to the user; do not silently continue or loop again |

The round bound is mandatory, and exhausting it is a reportable result, not a
reason to re-enter. A stalled `awaiting_initial_review` becomes
`timeout_eligible` once the creation-anchored deadline passes, so
`publish-timeout --if-eligible` can terminate a loop whose reviewer never
appeared.

Publish a timeout only when eligible:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" publish-timeout \
  REPO REVIEW_ID --if-eligible
```

Terminal inspection compares current source with the approved fingerprint. When
`approval_stale` is true, use its recommended `start-follow-up` command. The new
canonical document records `prior_review_id`; terminal history remains immutable.

## Safe External Validation

Prefer bounded metadata, HEAD/range, or short `ffprobe` probes. Record stable
resource identity, observation time, tool version, and sanitized fields, counts,
and outcome. A sanitized fixture may be an additional input; a raw signed
response must not be. Never persist signed URLs, query tokens, cookies, raw
headers or bodies, or an unredacted command in the event, report, receipt, or
source snapshot.

## Source Changes After a Terminal Event

LGTM and timeouts apply only to their recorded source fingerprint. If source
changes afterward, leave terminal history immutable and start a new loop with a
new ID. Mention the prior ID in the handoff. Never present the prior terminal
decision as approval of changed source.

Before reporting a loop complete, run terminal `inspect` and confirm
`approval_stale` is false. If it is true, run the recommended `start-follow-up`
before making any completion claim.
