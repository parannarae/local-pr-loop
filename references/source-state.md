# Source State and Publication

Commands assume the skill directory is `SKILL_DIR`. `REPO` may be any path
inside the target Git worktree; the helper resolves its root.

## Create an Isolated Loop

Ensure `REPO/.local/` is ignored, then initialize:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" init REPO feature-review
```

`init` returns an eight-character random `REVIEW_ID` and creates canonical JSON
plus the latest report beneath `.local/reviews/`. Keep the ID unchanged.

## Determine the Guarded Scope

Determine the comparison base from the user's request or pull-request metadata.
Ask when more than one base is plausible. Compute the merge base, then form the
candidate set from:

```bash
git merge-base HEAD BASE_REF
git diff --name-only MERGE_BASE..HEAD
git diff --name-only --cached
git diff --name-only
git ls-files --others --exclude-standard
```

Guard relevant modified implementation, tests, configuration, deployment
manifests, and guides. Read unchanged callers and dependencies as context without
adding them to scope. Add relevant ignored or generated files with
`--additional-input`.

## Inspect

```bash
bash "$SKILL_DIR/scripts/review-json.sh" inspect \
  REPO REVIEW_ID \
  --additional-input path/to/ignored-generated-config \
  path/to/source path/to/tests path/to/guide.md
```

`inspect` leads with projected workflow and derived operation status, then
prints canonical SHA-256 and the source snapshot. The snapshot covers scoped staged
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
bash "$SKILL_DIR/scripts/review-json.sh" lock acquire REPO REVIEW_ID
```

A reviewer making no source change may analyze first, but must acquire the lock
before `template`.

## Prepare and Validate an Event

```bash
bash "$SKILL_DIR/scripts/review-json.sh" template \
  REPO REVIEW_ID TOKEN owner_reply

bash "$SKILL_DIR/scripts/review-json.sh" validate-event REPO REVIEW_ID
```

Populate only the generated `.event.json`. Repeat `inspect` while holding the
lock and retain its exact canonical hash and source fingerprint.

Use the event kind and fields defined in
[review-schema.md](review-schema.md).

## Guarded Publication

```bash
bash "$SKILL_DIR/scripts/review-json.sh" publish \
  REPO REVIEW_ID TOKEN REVIEW_SHA SOURCE_FINGERPRINT \
  --additional-input path/to/ignored-generated-config \
  path/to/source path/to/tests path/to/guide.md
```

`publish` verifies lock ownership, canonical and source identities, validates
the event against immutable history, writes a durable receipt, and atomically
replaces canonical JSON. Canonical replacement is the sole commit point. Report
generation, draft removal, receipt removal, and tombstoned lock release are
recoverable cleanup.

Every result reports `committed`. After any nonzero result, inspect canonical
state before doing anything else. For `precommit_failed`, fix the problem and
reuse the unchanged draft or release the lock. For
`published_cleanup_required`, never retry the old canonical SHA; recover:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" recover-publish \
  REPO REVIEW_ID TOKEN
```

Recovery verifies the receipt event is already in canonical history, regenerates
the report, removes only a matching draft, releases the matching lock, and
removes the receipt without appending another event. If cleanup had already
released the lock, omit `TOKEN`.

Never remove another agent's lock or infer staleness from PID or elapsed time.
Release atomically renames the active lock to a unique inactive tombstone before
best-effort cleanup. Lock status omits the token; ask the user how to proceed.

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
