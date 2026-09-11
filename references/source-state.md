# Source State and Publication

Commands assume the skill directory is `SKILL_DIR`. `REPO` may be any path
inside the target Git worktree; the helper resolves its root.

## Discover Resumable Loops

When no review ID is supplied, determine the resumable loops from canonical
state:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" discover REPO [--json]
```

Only canonical `REVIEW_ID.json` files count as loops; auxiliary artifacts
(`.event.json`, `.latest.md`, `.lease.json`, `.guard.json`, `.publish.json`,
`.retired.json`) are ignored. Each canonical file is validated before it can be
a candidate: a corrupted or unsupported artifact is reported under `invalid`
and never selected, and terminal loops are listed under `terminal` and
excluded. Selection reads canonical content only, never file modification time.

The result reports one `status` with the matching `guidance`:

- `selected` — exactly one valid non-terminal loop; `selected_review_id` names
  it. Resuming still requires `inspect`: act only if your role's action is
  currently allowed.
- `ambiguous` — several valid non-terminal loops. Each candidate carries a
  compact summary (name, kind, phase, primary actor, threads, scope, latest
  event or creation time, source drift, lock). Ask the user to choose a review
  ID; never pick one automatically.
- `none` — no resumable loop. This alone never authorizes `init`; see the
  initialization rules in SKILL.md.

Per-candidate `source_drift` is `clean`, `drifted`, `no_snapshot` before any
snapshot event, or `unavailable` when the recorded scope can no longer be
snapshotted. `lock.held` is `true`, `false`, or `null` when status is
unavailable.

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

`inspect` leads with a role-aware action dashboard, one exact recommended
command, and the operating card for that command. Place a view flag immediately
after `REVIEW_ID`: `--agent` returns the operating card alone, which is the
ordinary agent read, and `--json` returns the full stable dashboard. The
dashboard carries the accretion ledger only where a `final_review` is allowed;
add `--accretion` to include it in any other phase. The snapshot covers scoped
staged and unstaged diffs, non-ignored untracked contents, and additional-input
metadata. A symlink digest covers its resolved regular-file content and records
its link target.

Use the same repository, scope, exclusions, and additional inputs for a
publication. If the reviewed source basis changes after review begins, the
reviewer publishes `source_update` with the replacement snapshot before owner
work continues. Comment on or reopen only the threads the replacement actually
affects, and open a new thread for each finding the replacement source raises.

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

## Open and Compose a Transaction

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" template \
  REPO REVIEW_ID owner_reply
```

`template` opens the draft under the lease. It stamps the envelope, fills in the
guarded snapshots, and prefills the operation skeletons whose set is already
known: one reply per open thread, one resolution per open gap, and the
`structure_debt` acknowledgment a flagged `final_review` owes. A review's
findings are not such a set, so a `review` draft opens with no operations.

Never edit the draft. Every act is one `draft` subcommand, which refuses bad
input as it is typed and names the argument at fault:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" draft REPO REVIEW_ID open-thread \
  --title "Reject malformed input" \
  --risk "Malformed input reaches persistence." \
  --required-behavior "Reject before persistence." \
  --paths src/service/ingest.py \
  --basis source_inspection \
  --provenance src/service/ingest.py \
  --sanitized-result "The guard runs after the write."

python3 "$SKILL_DIR/scripts/review_cli.py" draft REPO REVIEW_ID record-check \
  --check "focused tests" --result passed

python3 "$SKILL_DIR/scripts/review_cli.py" draft REPO REVIEW_ID open-gap \
  --check "live probe" --reason "service unavailable" --material

python3 "$SKILL_DIR/scripts/review_cli.py" draft REPO REVIEW_ID note T2 \
  --tag decision --message "bump deferred to the release commit"
```

The subcommands are `open-thread`, `reply`, `comment`, `resolve`, `reopen`,
`open-gap`, `resolve-gap`, `record-check`, `note`, `replace-source`, `approve`,
and `reply-context`. Each one's `--help` states its own arguments. The long
prose bodies also have file forms — `--message-file`, `--sanitized-result-file`,
and `reply-context`'s `--drift-file` and `--guide-file` — each reading from a
path, or from stdin for `-`, so a long reply costs its tokens once.

`note` attaches to a thread the draft already opens or acts on, so compose that
act first. Use it only for design-shifting changes, never mechanical fixes.

Read the threads a reply answers before composing it:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" threads REPO REVIEW_ID --json
```

`threads` returns full bodies by default, which is what drafting a reply needs.
Add `--summary` for identity, priority, status, title, and paths alone, and
`--open` to leave out threads that are already resolved; that pair is the
routing read.

Ask the draft what it still owes, then check the file as a whole:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" draft REPO REVIEW_ID show
python3 "$SKILL_DIR/scripts/review_cli.py" validate-event REPO REVIEW_ID
```

`draft show` names the operations composed so far and lists what remains
outstanding. `validate-event` is the belt rather than an authoring step: the
composer cannot write an invalid operation, but a file on disk can still be
corrupted between commands.

Every composer call restamps the draft's `occurred_at`, so evidence recorded
after templating never postdates its transaction. Never hand-edit the timestamp.

## Correct a Draft

Composing an act a second time corrects it in place. Taking one back is `drop`,
which names the operation exactly as `show` prints it:

```bash
python3 "$SKILL_DIR/scripts/review_cli.py" draft REPO REVIEW_ID drop \
  gap.resolve G1
```

This is how a prefilled resolution leaves a draft when its gap stays open. A gap
that is still material is not resolved at all, so the skeleton `template` wrote
for it can be filled only dishonestly; without `drop` the whole draft would have
to be aborted over one obligation that was never owed.

Removal reaches further than the operation named. A note goes with the thread act
it annotates, because a transaction may not carry a note for a thread it no
longer touches, and the `T<N>` and `G<N>` the draft opens are renumbered so the
sequence keeps no hole.

The same rule drops an operation without being asked: recording a check as passed
after recording it as failed removes the material gap the failure opened, which
would otherwise sit in the transaction contradicting it. Every acknowledgment
carries the count as `dropped`, so nothing leaves the draft silently.

`review.approve` and `source.replace` may not be dropped. They carry what
templating derived from the guard — the accretion ledger's flagged set and the
guarded snapshot — which composing them again would not restore, so correct them
with `approve` and `replace-source` instead.

The operation vocabulary and each kind's obligations are in
[review-schema.md](review-schema.md); routine authoring never needs to open it.

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

A deadline still ahead cuts the wait short, so eligibility is reported promptly.
A deadline already passed keeps reporting `deadline_reached` but no longer
shortens anything: the requested bound is waited in full and a change landing
inside it still returns `changed`, so a counterpart that is late rather than
absent can be waited for even though the deadline stays passed until the next
event lands.

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
| `timeout_eligible` | 4 | decide: `publish-timeout --if-eligible`, or another bounded wait |
| `exhausted` | 5 | report to the user; do not silently continue or loop again |

`timeout_eligible` permits, but does not require, a terminal timeout. Use it
when a counterpart has gone absent: the terminal is immutable, carries no
approval, and leaves open threads open. For a known delay, choose another
bounded wait. `exhausted` remains reportable rather than a reason to silently
start another cycle. A stalled `awaiting_initial_review` becomes eligible once
its creation-anchored deadline passes, so `publish-timeout --if-eligible` can
terminate a loop whose reviewer never appeared.

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
