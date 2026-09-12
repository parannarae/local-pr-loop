# Review Artifact Format

A review loop is one JSON document. Its `history` is an append-only list of
transactions, and every other field is derived from that list. This document
specifies what a transaction may contain, so that a person reading canonical
history by eye, or a tool author writing code that produces or consumes it,
can tell a valid artifact from an invalid one.

Routine authoring does not need this file. `inspect --agent` names the next
act with its obligations, the composer refuses bad input at entry, and
`--help` states each command's arguments. Read this when a transaction was
rejected and the reason is unclear, when auditing recorded history, or when
writing a tool against the format.

**Status.** The shipped parser, projection, renderer, and composer read and
write this format. A draft is opened by `template` and filled only by `draft`
subcommands; nothing in the agent path shapes this JSON by hand.

## Document envelope

The skill version is `0.10.0`. Persisted compatibility uses an independent
calendar revision:

```json
{
  "format": "local-pr-loop",
  "format_revision": "2026-09-11.1",
  "created_by": {"version": "0.10.0"},
  "created_at": "2026-08-15T01:00:00+00:00",
  "review_id": "k7m3q9wx",
  "prior_review_id": null,
  "name": "feature-review",
  "review_kind": "correctness",
  "structure_policy": "auto",
  "comparison_base": null,
  "state": {
    "workflow": {
      "phase": "awaiting_initial_review",
      "primary_actor": "reviewer",
      "primary_action": {"kind": "publish_initial_review"},
      "allowed_events_by_actor": {
        "reviewer": ["review", "final_review"]
      }
    },
    "source_fingerprint": null,
    "threads": {"open": [], "resolved": []},
    "validation_gaps": {"open": [], "resolved": []},
    "latest_event": null,
    "terminal": null
  },
  "history": []
}
```

`review_kind` is `correctness` or `structure`; `structure_policy` is `auto`,
`defer`, or `off`; `comparison_base` is null or the full merge-base commit SHA
recorded by `init --base-ref`. `review_id` and `prior_review_id` are
eight-character review IDs, and a follow-up never names itself as its prior
review. `name` is lowercase words joined by single hyphens.

### Frozen revisions

`format_revision` must equal the revision the running tools implement. Any
other value is rejected with guidance to preserve the artifact and start a new
loop; no migration code exists in either direction. An old loop therefore
stays readable as text and frozen as a loop.

The boundary fails closed: a reader refuses a document whose revision it does
not implement before interpreting any field. This matters most in the
direction that looks harmless — an operation-format transaction read by a
compound-format reader would find none of the fields it expects, read their
absence as empty defaults, and silently skip the publication gates that those
fields feed.

### Canonical state is a projection

`state` is recomputed from `history` and is valid only when it equals that
projection, so a document whose `state` was edited by hand is invalid rather
than merely stale. Everything a loop needs to route the next act — the phase,
the open and resolved thread and gap sets, the guarded fingerprint, the
terminal outcome — comes from replaying `history` and from nothing else.

## Transactions

A history entry is one transaction. It records a handoff between the two
agents, and it carries the operations that handoff performed:

```json
{
  "event_id": "evt_9f2c14ab77de",
  "kind": "review",
  "occurred_at": "2026-08-15T01:05:00+00:00",
  "source_snapshot": {"...": "..."},
  "operations": [
    {"op": "thread.open", "...": "..."}
  ]
}
```

- `event_id` is 12 to 64 characters of `[a-z0-9_-]`, starting alphanumeric,
  and is unique across the document.
- `kind` is one of `review`, `source_update`, `owner_reply`,
  `reviewer_update`, `final_review`, `owner_timeout`, `reviewer_timeout`,
  `initial_review_timeout`. It determines the acting agent, the phase the
  loop moves to, and which operations the transaction may carry.
- `occurred_at` is a timezone-aware ISO 8601 timestamp. Timestamps strictly
  increase along `history`, and none may sit more than five minutes ahead of
  the reader's clock.
- `operations` is an ordered list. Order is significant: thread and gap
  identifiers are assigned in the order the opening operations appear.

Which snapshot fields a transaction carries depends on its kind:

| Kind | Snapshot fields |
| --- | --- |
| `review`, `source_update`, `reviewer_update`, `final_review` | `source_snapshot` |
| `owner_reply` | `starting_source_snapshot`, `completed_source_snapshot` |
| timeouts | none |

### Handoff metadata on an owner reply

An `owner_reply` also carries four flat fields describing the work the whole
transaction handed back:

```json
{
  "source_drift_assessment": "...",
  "guide_synchronization": "...",
  "changed_files": ["..."],
  "revisions": ["..."]
}
```

These are envelope metadata, not operations, because they describe the
transaction as a whole rather than any one thread. They are mutable only
while the draft is held under a lease; once published they are immutable
canonical history. They stay flat because the transaction is already the
handoff, so nesting them would add a level that carries no distinction.

- `source_drift_assessment` is the owner's prose account of how the guarded
  source moved during the reply. It is distinct from the tool's computed
  `source_drift` status, which is operational and never stored.
- `guide_synchronization` is prose recording the established owner obligation
  of keeping project guides in step with the change.
- `changed_files` is a unique list of repository-relative paths. The name is
  modifier-first, matching the repository's convention.
- `revisions` is a unique list of full Git object IDs and is validated as
  such.

The composer derives `changed_files` and `revisions` from the repository.
The agent supplies only the two prose assessments, through message files.

## Operations

Every operation is one flat object with an `op` discriminator:

```json
{"op": "thread.comment", "thread_id": "T1", "message": "Still reproduces."}
```

The operation set is closed per revision. An unknown `op`, and an unknown
field inside a known `op`, are both rejected — the same rule that keeps raw
credentials, headers, and response payloads out of canonical history.

| Op | Fields beyond `op` |
| --- | --- |
| `thread.open` | `id`, `priority`, `contract`, `title`, `risk`, `required_behavior`, `paths`, `evidence`, `message?`, `anchors?` |
| `thread.reply` | `thread_id`, `decision`, `message`, `evidence`, `blocker?`, `completed_work?`, `remaining_work?`, `validation_gap?` |
| `thread.comment` | `thread_id`, `message` |
| `thread.resolve` | `thread_id`, `message`, `verification?` |
| `thread.reopen` | `thread_id`, `message` |
| `gap.open` | `gap_id`, `check`, `reason`, `material` |
| `gap.resolve` | `gap_id`, `disposition`, `message`, `evidence`, `justification?` |
| `check.record` | `check`, `result`, `evidence?` |
| `note.attach` | `target`, `tag`, `message` |
| `source.replace` | `snapshot`, `reason` |
| `review.approve` | `decision`, `structure_debt?` |
| `timeout.declare` | `started_at`, `deadline`, `reason` |

All thread lifecycle acts live under `thread.*`, including the owner's reply.

### Thread operations

`thread.open` raises a finding:

```json
{
  "op": "thread.open",
  "id": "T1",
  "priority": "P1",
  "contract": "external",
  "title": "Reject malformed input",
  "risk": "Malformed input reaches persistence.",
  "required_behavior": "Reject before persistence.",
  "paths": ["src/service/ingest.py"],
  "evidence": {"...": "..."},
  "anchors": [
    {"path": "src/service/ingest.py", "start_line": 42, "end_line": 58}
  ]
}
```

`priority` is `P0` through `P3` and `contract` is `internal` or `external`.
`paths` names the repository-relative files the finding concerns; the
accretion ledger counts raised threads per guarded file, so an empty `paths`
hides the finding from the ledger, and a path outside the guarded scope never
participates. `message` is optional prose that flags the finding at raise
time.

`anchors` is optional. Each entry pins a 1-based inclusive line range in one
file, read against the `revision` of the transaction's guarded snapshot, so
the anchor stays meaningful after the working tree moves on. Every anchored
`path` must also appear in `paths`, `end_line` must not precede `start_line`,
and entries must be unique. This is the one capability the operation format
adds; nothing else in the vocabulary is more than a normalization of what the
compound format already stored.

`thread.reply` is the owner's answer to one open thread. `decision` is
`applied`, `declined`, or `deferred/blocked`; the last requires `blocker`,
`completed_work`, `remaining_work`, and `validation_gap`, each non-empty
prose.

`thread.comment` records a reviewer or source-update observation that leaves
the thread open.

`thread.resolve` closes an open thread. It carries `verification` when the
reviewer is overriding the owner: if the thread's most recent `thread.reply`
declined, the resolution must carry independent verification, and a
resolution without it is rejected.

```json
{
  "op": "thread.resolve",
  "thread_id": "T1",
  "message": "Verified through a different interface.",
  "verification": {
    "independent": true,
    "evidence": {"...": "..."}
  }
}
```

If independent verification is unavailable, use `thread.comment`, record the
exact unavailable check as a `gap.open`, and leave the thread open.

`thread.reopen` returns a resolved thread to open. It is rejected against a
thread that is already open.

### Validation operations

`check.record` records a check that was actually run. `result` is `passed` or
`failed`, and a failed check requires a material `gap.open` in the same
transaction naming the same check.

```json
{
  "op": "check.record",
  "check": "bounded metadata probe",
  "result": "passed",
  "evidence": {"...": "..."}
}
```

`gap.open` records a check that was not run. `material` is a boolean: a
material gap blocks LGTM until it is resolved.

```json
{
  "op": "gap.open",
  "gap_id": "G1",
  "check": "full media download",
  "reason": "Intentionally bounded to ten seconds.",
  "material": false
}
```

`gap.resolve` disposes of a gap opened by an earlier transaction, and only
`reviewer_update` and `final_review` may carry it. `disposition` is one of:

- `performed`: the check was finally run, and the evidence records its
  result.
- `unavailable_non_material`: the check still was not run. This requires a
  `justification` recording `unperformed_check` and `fail_closed_behavior`,
  alongside the resolution's own evidence for the residual-risk assessment.
  The report renders it as resolved *without* performing the check and quotes
  those recorded facts, so the rendered claim never exceeds what the
  transaction carries. A `performed` disposition rejects `justification`,
  which does not apply to it.

A resolution must name a gap that is currently open, and one transaction may
resolve a given gap only once.

A still-material gap has no disposition, because it is not resolved: it stays
open and blocks LGTM.

### Notes

`note.attach` records something the user must see. It replaces the
`Note to user:` prose marker that the old format buried inside messages, so a
note is a typed record rather than a parsed line.

```json
{
  "op": "note.attach",
  "target": {"kind": "thread", "id": "T2"},
  "tag": "decision",
  "message": "Bump deferred to the release commit."
}
```

`tag` is `action-required`, `follow-up`, or `decision`. The summary report
lifts each note into its first section. Use notes for design, contract, or
business-logic shifts and decisions that need the user's attention; never for
mechanical fixes. Blocked replies, timeout terminals, and material gaps open
at terminal surface there automatically, without a note.

`target` is a closed object. In this revision its only legal form is
`{"kind": "thread", "id": "T<N>"}`, which matches what notes could attach to
before. The general name and the object shape are deliberate: a review-level
or gap-level note is a new capability and must arrive through a future format
revision, not through a field that was reserved for it in advance.

### Source, approval, and timeout

`source.replace` records that a reviewer replaced the guarded basis. Its
`snapshot` is the new basis, and `reason` states why the scope or revision
moved. The transaction's envelope `source_snapshot` carries the same basis,
because the guard and drift machinery reads it there for every kind; the two
must be identical, and a `source_update` whose operation and envelope disagree
is rejected.

`review.approve` records the approval on a `final_review`. `decision` is
non-empty prose. `structure_debt` is present only when the accretion ledger
flagged files, under the rule below.

`timeout.declare` terminates an abandoned handoff:

```json
{
  "op": "timeout.declare",
  "started_at": "2026-08-15T01:05:00+00:00",
  "deadline": "2026-08-15T03:05:00+00:00",
  "reason": "The active handoff deadline elapsed without a response."
}
```

It does not restate which timeout this is. The enclosing transaction kind
already determines the acting agent, the fixed duration, and the phase
transition, so a repeated classification field could only ever disagree with
it.

## Shared value shapes

### Evidence

```json
{
  "basis": "captured_fixture",
  "provenance": "sanitized fixture fixtures/rejection.json",
  "observed_at": "2026-07-25T03:00:00+00:00",
  "sanitized_result": "The service accepted the malformed field.",
  "artifact_digest": "lowercase-sha256-when-present"
}
```

`basis` is `source_inspection`, `test_result`, `live_probe`,
`captured_fixture`, or `authoritative_contract`. A `captured_fixture` basis
requires `artifact_digest`. `observed_at` is timezone-aware and may not
postdate the `occurred_at` of the transaction carrying it — evidence cannot be
observed after the transaction that reports it.

A `thread.open` whose `contract` is `external` and whose `priority` is `P1` or
`P2` must cite `live_probe`, `captured_fixture`, or `authoritative_contract`.
A synthetic counterexample is not enough for a claim about an external
contract.

Never record credentials, signed URLs, query tokens, cookies, raw responses,
raw headers, or unredacted commands anywhere in a transaction.

### Source snapshot

A snapshot pins the guarded tree: `revision` (a full Git object ID), a
non-empty unique `scope`, unique `exclusions`, `additional_inputs`,
`fingerprint`, `staged_sha256`, `unstaged_sha256`, and `untracked`. Each
entry of `additional_inputs` and `untracked` carries `path`, `kind` (`file`
or `symlink`), a four-digit octal `mode`, `sha256`, and `link_target` for a
symlink; paths within a list are unique. [source-state.md](source-state.md)
covers how snapshots are taken and compared.

Two derived comparisons matter to transaction rules. Snapshot *identity* is
`revision`, `scope`, `exclusions`, `additional_inputs`, and `fingerprint`
together. The *scope basis* is `scope`, `exclusions`, and the paths of
`additional_inputs` — the declaration, without the content digests that change
as the owner works.

### Structure debt

```json
{
  "disposition": "structure_deferred",
  "flagged_paths": ["src/service/reconciler.py"],
  "message": "Real accretion; a structure round should follow."
}
```

`structure_reviewed` records the judgment that the flags do not reflect real
accretion, or that a structure round already covered them. `structure_deferred`
records real debt left for a later round and surfaces in the report's notes.

## Transaction rules by kind

Each kind admits a specific set of operations and carries obligations that are
checked when the transaction is published.

Three operations are allowed in every kind except a timeout, so the rules
below do not repeat them: `check.record` and `gap.open` record what this
transaction validated, and `note.attach` may target any thread this
transaction opens or acts on.

**`review`** — opens the loop and routes to the owner. One or more
`thread.open`.

**`source_update`** — replaces the guarded basis and routes to the owner. One
`source.replace`; `thread.comment` and `thread.reopen` as impacts;
`thread.open` for findings the new basis raises.

**`owner_reply`** — exactly one `thread.reply` for every open thread, and none
for a thread that is not open. No `thread.open`, `thread.resolve`, or
`thread.reopen`: the owner never opens or closes a thread.
`starting_source_snapshot` must match the current guarded snapshot by
identity, and `completed_source_snapshot` must keep the same scope basis —
changing the declaration is a `source_update`, not a reply. The four handoff
metadata fields above are required.

**`reviewer_update`** — every open thread receives a `thread.comment` or a
`thread.resolve`; `thread.reopen` may target a resolved thread; `thread.open`
and `gap.resolve` allowed. At least one thread must remain open when the
transaction ends — use `final_review` when nothing is left open.
`source_snapshot` must match the current guarded snapshot by identity.

**`final_review`** — every open thread receives a `thread.resolve`, exactly
one `review.approve` is present, and the snapshot rule of `reviewer_update`
applies unless this is the first transaction in the loop. `gap.resolve` is
allowed. LGTM is refused while any `check.record` in the transaction failed,
or while any material gap anywhere in the loop is still open. Non-material
gaps may stay open.

**Timeouts** — exactly one `timeout.declare` and nothing else.
`reviewer_timeout` runs 30 minutes, `owner_timeout` and
`initial_review_timeout` two hours. `deadline` must equal `started_at` plus
that duration, and `occurred_at` must not precede `deadline`. `started_at`
must equal the anchor of the handoff being abandoned: the `occurred_at` of the
latest transaction, or the document's `created_at` for
`initial_review_timeout`, whose handoff begins before any transaction exists.

A transaction after a terminal one is rejected; `final_review` and the three
timeouts are terminal.

## Rules that hold across every transaction

**Actor authority.** Kind determines actor: the reviewer publishes `review`,
`source_update`, `reviewer_update`, `final_review`, and `owner_timeout`; the
owner publishes `owner_reply`, `reviewer_timeout`, and
`initial_review_timeout`. Phases are `awaiting_initial_review`,
`owner_response`, `reviewer_verification`, and `terminal`. Within a phase,
`allowed_events_by_actor` is authoritative, and `primary_actor` describes the
main handoff without excluding the other allowed transactions.

**Identifiers.** Thread IDs are `T<N>` and gap IDs are `G<N>`, both globally
sequential and gap-free across the whole document, assigned in the order their
opening operations appear. A `thread.open` or `gap.open` carrying anything but
the next expected identifier is rejected, so identifiers stay stable for the
life of the loop.

**One act per thread.** A transaction may open, reply to, comment on, resolve,
or reopen a given thread once. A handoff that acted twice would record two
answers to the same finding with nothing to say which one it meant, so the
second act is rejected rather than allowed to win.

**Closed field sets.** The document, every transaction, every operation, and
every nested record reject unknown fields. Reading canonical history also
rejects duplicate JSON keys, so a repeated field cannot decide a rule by
parser accident.

**Accretion and structure debt.** The ledger is derived state, never written
to history. It flags a guarded file when raised threads name it at least five
times, or when its net line growth from `comparison_base` to the working tree
exceeds 20% of its base line count, both confined to the guarded scope minus
exclusions. A file absent or empty at the base is authored whole on the branch
and is judged by the reviewer rather than auto-flagged; an unreachable base
degrades to the thread signal. A correctness `final_review` over flagged files
must carry `structure_debt` on its `review.approve`, with `flagged_paths`
matching the flagged set exactly. Presence and the flagged set are enforced at
publish time, where the source fingerprint pins the guarded tree; projection
never re-checks them, so a terminal document stays valid after the tree moves
on. A `structure` loop may never record `structure_debt` — the acknowledgment
belongs to the correctness loop that flagged the files.

## What never enters history

Drafts, locks and leases, publication receipts, generated reports, and the
question of whether a deadline has become eligible are operational state.
`inspect` derives one artifact status from them — `clean`, `editing_draft`,
`ready_to_publish`, `prepared_precommit`, `committed_cleanup`, `stale_report`,
or `corrupt_artifact` — with lock status reported separately, because a locked
valid draft is still `ready_to_publish`. None of this is canonical, and
wall-clock eligibility never changes canonical state.
[source-state.md](source-state.md) covers snapshots, publication, and
recovery.

## Moving from the compound format

[operation-format-parity.md](operation-format-parity.md) records what each
compound field became, which behavioral tests carry over to the operation
format, and in which step each one is ported.
