# Source State and Publication

Commands assume the skill directory is `SKILL_DIR`. `REPO` may be any path
inside the target Git worktree; the helper resolves its root.

## Create an Isolated Loop

Ensure `REPO/.local/` is ignored by Git, then initialize:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" init REPO feature-review
```

The command returns an eight-character random `REVIEW_ID`; the descriptive name
is stored inside canonical JSON. It creates canonical JSON and the latest report
under that repository's `.local/reviews/`. Keep the ID unchanged for all later
commands. A separate checkout or worktree has its own `.local/reviews/`, and a
separate invocation receives a different ID.

## Inspect

Declare every repository-relative implementation, caller, test, configuration,
and guide path that can affect the reviewed behavior:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" inspect \
  REPO REVIEW_ID \
  --additional-input path/to/ignored-generated-config \
  path/to/source path/to/tests path/to/guide.md
```

The command prints artifact paths, routing state, canonical `review_sha256`,
lock status, revision, and source `fingerprint`. The snapshot includes scoped
staged and unstaged diffs, non-ignored untracked-file contents, and each
declared `--additional-input` digest. Use that option for relevant ignored or
generated source inputs; it records metadata and a digest, not secret content.
For a symlink, the digest covers its resolved regular file and records its link
target.

Use the same repository, scope, exclusions, and additional inputs throughout an
iteration.

## Lock Before Mutation

Acquire the cooperative lock before changing canonical JSON, creating the
event, or changing declared source:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" lock acquire REPO REVIEW_ID
```

A reviewer making no source change may finish analysis first, but must acquire
the lock before `template`.

## Prepare and Validate an Event

```bash
bash "$SKILL_DIR/scripts/review-json.sh" template \
  REPO REVIEW_ID TOKEN owner_response 2

bash "$SKILL_DIR/scripts/review-json.sh" validate-event REPO REVIEW_ID
```

Populate the returned `.event.json`, then repeat `inspect` while holding the
lock. Use its final canonical hash and source fingerprint for publication.

## Guarded Publication

```bash
bash "$SKILL_DIR/scripts/review-json.sh" publish \
  REPO REVIEW_ID TOKEN REVIEW_SHA SOURCE_FINGERPRINT \
  --additional-input path/to/ignored-generated-config \
  path/to/source path/to/tests path/to/guide.md
```

`publish` verifies lock ownership, canonical and source identities, validates
and appends the event, derives routing state, regenerates the latest Markdown
report, removes the temporary event, and releases the lock.

If publication fails, preserve the event, release the held lock, and reassess:

```bash
bash "$SKILL_DIR/scripts/review-json.sh" lock release \
  REPO REVIEW_ID TOKEN
```

Honor the lock before changing canonical JSON, event JSON, or active source.
Never remove another agent's lock or infer staleness from elapsed time. Lock
status omits the release token; ask the user how to proceed.

Review IDs isolate artifacts, not worktree changes. Do not run concurrent loops
whose declared source scopes overlap in the same worktree.
