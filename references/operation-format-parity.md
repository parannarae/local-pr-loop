# Operation Format Capability Parity

Moving from the compound event format to the operation format is breaking by
design: no artifact migrates, and the old revision stays readable-but-frozen.
Behavior is a different matter. Every behavior the compound format enforced
has to survive, and the way to know it survived is to name each existing
behavioral test now and say where its successor lands — rather than discover
the gaps once the tests are gone.

This file is the record. It is development bookkeeping for the format change,
not a reference a review loop reads;
[review-schema.md](review-schema.md) is the format itself.

**Status.** Every suite below has been ported to the operation format — the
step 3 ones through the composer, the step 4 ones by moving their fixtures to
`operations`. The only gate this file cannot close is the dogfood loop, which
is recorded at the end.

## What each compound field became

| Compound location | Operation format |
| --- | --- |
| `review.threads[]`, `new_threads[]` | `thread.open` |
| `owner_reply.replies[]` | `thread.reply` |
| `decisions[]`/`thread_impacts[]` with `action: comment` | `thread.comment` |
| `decisions[]` with `action: resolve`, `final_review.resolutions[]` | `thread.resolve` |
| `decisions[]`/`thread_impacts[]` with `action: reopen` | `thread.reopen` |
| `validation.performed[]` | `check.record` |
| `validation.gaps[]` | `gap.open` |
| `gap_resolutions[]` | `gap.resolve` |
| `Note to user:` lines inside any message | `note.attach` |
| `source_update.source_snapshot` and `reason` | `source.replace` |
| `final_review.decision` and `structure_debt` | `review.approve` |
| timeout `started_at`, `deadline`, `reason` | `timeout.declare` |
| `owner_reply.source_drift_assessment`, `guide_synchronization` | envelope metadata, same names |
| `owner_reply.files_changed` | envelope metadata `changed_files` |
| `owner_reply.commits` | envelope metadata `revisions` |
| event snapshot fields | envelope, unchanged |

## Deliberate capability changes

Everything above is a normalization that preserves behavior. These five are
decisions in their own right, not consequences of the normalization:

1. **`anchors` is added** to `thread.open`: optional line ranges against the
   guarded snapshot's revision. It has no predecessor and therefore no
   existing test; it needs new coverage in the step that implements it.
2. **A note must declare a `tag`.** The values are unchanged
   (`action-required`, `follow-up`, `decision`), but `add-note --tag` was
   optional and the operation's field is required. An untagged note has no
   successor form.
3. **`revisions` is validated as full Git object IDs**, where `commits`
   accepted any unique non-empty string.
4. **A note is a record, not a marker.** The report's notes section is
   sourced from `note.attach` instead of parsing `Note to user:` out of
   message prose, so a note can no longer be written or removed by editing a
   message body.
5. **`timeout.declare` does not restate its kind.** No behavior changes: the
   classification already came from the enclosing event kind.

Plan 112's interim owner-reply source-replacement field is deliberately
absent; this format change does not depend on it.

## Test inventory

The unit is a test function as written: **252 test functions across 20
files**. Nothing is parametrized, so functions and cases match everywhere
except `test_review_budgets.py`, which fans each function out over the
budgeted views with `subTest`.

"Ported in" is the step that must carry the successor. A suite listed as
carrying over unchanged is still a gate: it has to keep passing, and it is not
evidence of operation-format behavior.

### Ported with the low-level format (step 2)

Schema, projection, and render parity, asserted against constructed
histories.

| Suite | Tests | What it pins |
| --- | --- | --- |
| `test_review_state.py` | 28 | Envelope and event validation, projection, workflow routing, evidence rules, independent verification, material gaps, frozen revisions, structure fields |
| `test_review_render.py` | 25 | Report and thread views: summaries, conversations, notes, declined labels, verification roll-up, structure debt |
| `test_review_gap_disposition.py` | 12 | Gap dispositions, justification shape, and the claim the report is allowed to make |

**65 tests.** Their fixtures are compound events written inline, so each one
is re-authored against `operations` rather than adapted.

Two suites need a smaller touch in the same step, because they construct
events but assert machinery rather than format:
`test_review_recovery.py` (7, timeout draft publishability and recorded
outcomes) and `test_review_workflow.py` (7, wait bounds against handoff
deadlines).

Step 2 also adds coverage with no predecessor: the revision boundary must
refuse an operation-format document to a reader that does not implement the
revision, and refuse a compound document to one that does.

### Ported with the composer (step 3)

| Suite | Tests | What it pins |
| --- | --- | --- |
| `test_review_json_cli.py` | 22 | End-to-end CLI: publish, await-handoff, notes, templates, scope candidates, ledger emission, thread views |
| `test_review_card.py` | 22 | Operating card: phase obligations, routed references, recommended commands |
| `test_review_budgets.py` | 4 | Byte ceilings on the compact views |
| `test_review_cli.py` | 3 | Public command model and version-string consistency |
| `test_review_module_boundaries.py` | 4 | Import direction across layers |

**55 tests.** These converted rather than ported: the card recommends composer
commands, the CLI tests drive `draft` subcommands instead of hand-populating
drafts, and the boundary test gained the composer module. Budgets extended to
composer acknowledgments and did not grow against the 103-era numbers.
`test_review_cli.py`'s version assertions move again in step 5, when the skill
version bumps.

Step 3 also adds 39 tests with no predecessor. Thirty-two of them are
`test_review_compose.py`, which pins the entry-time refusals the plan names —
evidence of the wrong basis, evidence observed after its handoff, resolving the
last open thread outside a `final_review` — plus identifier assignment and the
one-act-per-thread slot. The remaining seven extend the ported suites: composed
drafts and `draft show` end to end, a typed constructor for every operation in
the vocabulary, the composer's place in the import graph, and the acknowledgment
budgets.

### Ported with the remaining machinery (step 4)

| Suite | Tests | What it pins |
| --- | --- | --- |
| `test_review_ledger.py` | 30 | Accretion signals, flagged sets, structure-debt enforcement, follow-up due-ness |
| `test_review_terminal.py` | 16 | Terminal reporting and follow-up convergence |
| `test_review_discover.py` | 16 | Candidate selection, validity reporting, drift and lock status |

**62 tests.** All three read history, and the first two read thread paths and
`structure_debt` out of it, so their fixtures move when their readers do.
`test_review_discover.py` converted rather than ported: its published-review
fixture now composes the thread through `draft` instead of writing one into the
template.

Three suites listed as unaffected turned out to need a fixture touch, and are
carried here. `test_review_overlap.py` (9) and `test_review_retire.py` (8) each
build a terminal through `append_event` from a hand-populated timeout, so their
`reason`, `started_at`, and `deadline` move into `timeout.declare`.
`test_review_publish.py` (11) injects faults around a `final_review` whose
approval decision moves onto `review.approve`. The behavior all three assert —
guard overlap, retirement eligibility, and the commit point — is untouched.

### Ported with the docs and version bump (step 5)

`test_review_cli.py`'s version assertions moved with the skill version to
`0.10.0`; the suite itself was already ported in step 3.

### Carrying over unchanged

| Suite | Tests | Why it is unaffected |
| --- | --- | --- |
| `test_review_scope.py` | 17 | Scope declaration and path canonicalization, below the event format |
| `test_review_drift.py` | 5 | Snapshot drift detail; envelope snapshots are unchanged |
| `test_review_lock.py` | 3 | Lock acquisition and release |
| `test_review_io.py` | 3 | Permission-restricted file handling |

**28 tests.**

### Counts

| Disposition | Tests |
| --- | --- |
| Ported in step 2 | 65, plus 14 fixture-level touches |
| Ported in step 3 | 55 |
| Ported in step 4 | 62, plus 28 fixture-level touches |
| Unchanged | 28 |
| Total | 252 |

## Exceptions

No existing behavioral test is expected to lose its behavior. The two
closest calls both convert rather than retire: the marker-parsing tests in
`test_review_render.py` become assertions that notes come from `note.attach`,
and `add-note` coverage in `test_review_json_cli.py` becomes coverage of the
composer command that appends the operation.

An exception discovered while porting is recorded here with the behavior it
covered and the reason that behavior no longer exists — in the step that
finds it, not at the end.

**Step 3: the blank thread skeleton is gone, and with it its test.** A `review`
template used to prefill one empty `thread.open`, and a test pinned its empty
`paths` list as the prompt for the agent to name files. A review's findings are
not a set the template can know, so it now opens with no operations at all and
every thread arrives through `draft open-thread`, which requires `--paths`. The
successor is that requirement, asserted where the composer refuses a thread
without it.

**Step 2: the blocked-alert duplicate suppression is gone.** The compound
renderer dropped its automatic `[blocked]` note when a marked note in the same
event was an exact normalized copy of it, because both were free prose and an
agent could restate the blocker by hand. A `note.attach` carries a closed tag
from `action-required`, `follow-up`, `decision`, so its rendered text can never
begin `[blocked]` and the duplicate is unreachable. The blocked-work alert now
always surfaces, and the successor test asserts that an unrelated note on a
blocked thread does not hide it.

## Gates this inventory does not satisfy

Naming a successor is not running one. One gate stays open and a document
cannot close it: a full dogfood loop on the new format, including one structure
round and one timeout path.
