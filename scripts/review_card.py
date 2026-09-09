"""Build the phase-scoped operating card `inspect` hands the acting agent.

The card is control, not content: every value below is a stable identifier an
agent or the skill's own code can branch on, and the sentences that explain
those identifiers live in `SENTENCE_BY_*` for the human renderer alone. Agents
that matched on prose would freeze the wording, and the card exists to be
retuned as agents fail in new ways.
"""

from __future__ import annotations

from typing import Any

SCHEMA_REFERENCE = "references/review-schema.md"
SOURCE_REFERENCE = "references/source-state.md"
STRUCTURE_REFERENCE = "references/structure-review.md"

# The action the card describes, keyed by the artifact condition that produced it.
# Local artifacts are repaired before the phase's own event, so these outrank the
# phase actions below.
ACTION_BY_OPERATION_STATUS = {
    "committed_cleanup": "recover_publication",
    "prepared_precommit": "recover_publication",
    "stale_report": "regenerate_report",
    "editing_draft": "abort_draft",
    "corrupt_artifact": "abort_draft",
    "ready_to_publish": "publish_draft",
}

ACTION_BY_PHASE = {
    "awaiting_initial_review": "publish_initial_review",
    "owner_response": "publish_owner_reply",
    "reviewer_verification": "publish_reviewer_update",
    "terminal": "none",
}

LOCK_STEPS = ("acquire_lock", "inspect_under_lease_with_scope")

# Templating through publication is one sequence whatever event kind it carries;
# the kind itself is already named by the card's action and next command.
AUTHORING_STEPS = (
    "template_event",
    "populate_draft_blanks",
    "record_validation_evidence",
    "validate_event",
    "inspect_before_publish",
    "publish",
    "read_publication_result",
    "await_handoff",
)

STEPS_BY_ACTION = {
    "wait_for_lock": ("wait", "inspect"),
    "recover_publication": ("recover_publish", "inspect"),
    "regenerate_report": ("regenerate_report", "inspect"),
    "abort_draft": ("abort_draft", "inspect"),
    "publish_draft": (
        "inspect_before_publish",
        "publish",
        "read_publication_result",
        "await_handoff",
    ),
    "start_follow_up": ("start_follow_up", "inspect_successor"),
    "start_structure_follow_up": ("start_follow_up", "inspect_successor"),
    "none": (),
    "publish_initial_review": AUTHORING_STEPS,
    "publish_owner_reply": AUTHORING_STEPS,
    "publish_reviewer_update": AUTHORING_STEPS,
    "publish_source_update": AUTHORING_STEPS,
}

OBLIGATIONS_BY_ACTION: dict[str, dict[str, tuple[str, ...]]] = {
    "publish_initial_review": {
        "must": (
            "declare_complete_guarded_scope",
            "name_paths_on_every_thread",
            "record_evidence_for_external_contract_findings",
            "await_handoff_if_not_primary_actor",
        ),
        "must_not": (
            "transcribe_another_agents_findings",
            "publish_lgtm_with_material_validation_gap",
        ),
    },
    "publish_owner_reply": {
        "must": (
            "reply_to_every_open_thread",
            "state_decision_on_every_reply",
            "record_evidence_for_declined_work",
            "flag_design_shift_with_add_note",
            "await_handoff_if_not_primary_actor",
        ),
        "must_not": (
            "resolve_thread",
            "reopen_thread",
            "open_new_thread",
            "hand_edit_canonical_json",
        ),
    },
    "publish_reviewer_update": {
        "must": (
            "decide_every_open_thread",
            "verify_declined_thread_independently_before_resolving",
            "resolve_every_open_thread_in_final_review",
            "flag_design_shift_with_add_note",
            "await_handoff_if_not_primary_actor",
        ),
        "must_not": (
            "transcribe_another_agents_findings",
            "publish_lgtm_with_open_thread",
            "publish_lgtm_with_material_validation_gap",
        ),
    },
    "publish_source_update": {
        "must": (
            "record_only_actual_thread_impacts",
            "open_new_thread_for_each_finding_in_the_replacement_source",
            "await_handoff_if_not_primary_actor",
        ),
        "must_not": ("resolve_thread_on_source_change_alone",),
    },
    "publish_draft": {
        "must": (
            "read_publication_result_by_committed_first",
            "await_handoff_if_not_primary_actor",
        ),
        "must_not": ("republish_a_committed_event", "hand_edit_canonical_json"),
    },
    "recover_publication": {
        "must": ("read_publication_result_by_committed_first",),
        "must_not": ("republish_a_committed_event", "hand_edit_canonical_json"),
    },
    "abort_draft": {
        "must": ("template_again_rather_than_repairing_the_rejected_draft",),
        "must_not": ("hand_edit_canonical_json",),
    },
    "regenerate_report": {
        "must": ("generate_the_report_rather_than_hand_editing_it",),
        "must_not": ("hand_edit_canonical_json",),
    },
    "wait_for_lock": {
        "must": ("re_arm_the_wait_until_the_lock_clears",),
        "must_not": ("break_another_agents_lock", "hand_edit_canonical_json"),
    },
    "start_follow_up": {
        "must": (
            "inspect_the_successor_before_acting_on_it",
            "adopt_the_scope_the_prior_review_guarded",
        ),
        "must_not": ("guard_an_inherited_loop_with_a_scope_you_chose",),
    },
    "start_structure_follow_up": {
        "must": (
            "inspect_the_successor_before_acting_on_it",
            "adopt_the_scope_the_prior_review_guarded",
        ),
        "must_not": ("guard_an_inherited_loop_with_a_scope_you_chose",),
    },
}

TERMINAL_OBLIGATIONS = {
    "must": ("confirm_approval_stale_is_false_before_reporting_complete",),
    "must_not": ("publish_after_terminal", "hand_edit_canonical_json"),
}

# Reached only where no action is available outside a terminal, which leaves nothing
# for the agent to do but keeps canonical history off limits.
IDLE_OBLIGATIONS = {
    "must": (),
    "must_not": ("hand_edit_canonical_json",),
}

# Each flag suspends the routine sequence and names what the agent must read
# instead. Ordered most-blocking first so the human renderer reads that way too.
REFERENCE_BY_FLAG = {
    "publication_recovery": SOURCE_REFERENCE,
    "corrupt_artifact": SOURCE_REFERENCE,
    "source_drift": SOURCE_REFERENCE,
    "scope_undeclared": SOURCE_REFERENCE,
    "scope_changed": SOURCE_REFERENCE,
    "timeout_eligible": SOURCE_REFERENCE,
    "open_validation_gaps": SCHEMA_REFERENCE,
    "structure_round": STRUCTURE_REFERENCE,
    "structure_follow_up_due": STRUCTURE_REFERENCE,
}

SENTENCE_BY_STEP = {
    "acquire_lock": "acquire the lock",
    "inspect_under_lease_with_scope": (
        "inspect again under the lease with the same scope, which writes the guard"
    ),
    "template_event": "create the draft with template",
    "populate_draft_blanks": "fill in the draft's semantic blanks",
    "record_validation_evidence": (
        "record validation with add-check, add-gap, and evidence-template"
    ),
    "validate_event": "run validate-event and fix what it reports",
    "inspect_before_publish": "inspect once more",
    "publish": "publish",
    "read_publication_result": "read the publication result by committed first",
    "await_handoff": "run await-handoff and act on its outcome",
    "wait": "wait for the lock holder",
    "inspect": "inspect again",
    "recover_publish": "run recover-publish",
    "regenerate_report": "run regenerate-report",
    "abort_draft": "run abort-draft",
    "start_follow_up": "run the recommended start-follow-up",
    "inspect_successor": "inspect the successor before acting on it",
}

SENTENCE_BY_MUST = {
    "declare_complete_guarded_scope": "declare the complete guarded scope",
    "name_paths_on_every_thread": "name the files each finding concerns in its paths",
    "record_evidence_for_external_contract_findings": (
        "record real evidence for external-contract P1 and P2 findings"
    ),
    "await_handoff_if_not_primary_actor": (
        "run await-handoff instead if you hold the other role"
    ),
    "reply_to_every_open_thread": "reply to every open thread",
    "state_decision_on_every_reply": (
        "give every reply a decision of applied, declined, or deferred/blocked"
    ),
    "record_evidence_for_declined_work": "record evidence for work you decline",
    "flag_design_shift_with_add_note": (
        "flag a design, contract, or business-logic shift with add-note"
    ),
    "decide_every_open_thread": "decide every open thread",
    "verify_declined_thread_independently_before_resolving": (
        "verify a declined thread independently before resolving it"
    ),
    "resolve_every_open_thread_in_final_review": (
        "resolve every remaining open thread if you publish final_review"
    ),
    "record_only_actual_thread_impacts": "record only thread impacts that actually occurred",
    "open_new_thread_for_each_finding_in_the_replacement_source": (
        "open a new thread for each finding in the replacement source"
    ),
    "confirm_approval_stale_is_false_before_reporting_complete": (
        "confirm approval_stale is false before reporting the loop complete"
    ),
    "read_publication_result_by_committed_first": (
        "read the publication result by committed first"
    ),
    "acknowledge_structure_debt": (
        "carry the structure_debt acknowledgment the final_review template prefills"
    ),
    "template_again_rather_than_repairing_the_rejected_draft": (
        "template the event again rather than repairing the rejected draft"
    ),
    "generate_the_report_rather_than_hand_editing_it": (
        "generate the report rather than hand-editing it"
    ),
    "re_arm_the_wait_until_the_lock_clears": (
        "keep re-arming the wait until the lock clears"
    ),
    "inspect_the_successor_before_acting_on_it": (
        "inspect the successor and confirm its name, kind, and prior review"
    ),
    "adopt_the_scope_the_prior_review_guarded": (
        "adopt the scope the prior review guarded"
    ),
}

SENTENCE_BY_MUST_NOT = {
    "transcribe_another_agents_findings": "transcribe another agent's findings",
    "publish_lgtm_with_material_validation_gap": (
        "publish LGTM with a material validation gap"
    ),
    "publish_lgtm_with_open_thread": "publish LGTM with a thread still open",
    "resolve_thread": "resolve a thread",
    "reopen_thread": "reopen a thread",
    "open_new_thread": "open a new thread",
    "hand_edit_canonical_json": "hand-edit canonical JSON",
    "resolve_thread_on_source_change_alone": (
        "resolve a thread because the source changed"
    ),
    "publish_after_terminal": "publish anything after a terminal event",
    "republish_a_committed_event": "republish an event that already committed",
    "break_another_agents_lock": "break another agent's lock",
    "guard_an_inherited_loop_with_a_scope_you_chose": (
        "guard an inherited loop with a scope you picked yourself"
    ),
}

SENTENCE_BY_FLAG = {
    "publication_recovery": "a publication is part-way through and must be recovered",
    "corrupt_artifact": "a local artifact is unreadable",
    "source_drift": "the guarded source moved since it was recorded",
    "scope_undeclared": "this loop has not declared its scope yet",
    "scope_changed": "the declared scope differs from the recorded one",
    "timeout_eligible": "a timeout event is now eligible",
    "open_validation_gaps": "validation gaps are open and block LGTM",
    "structure_round": "this is a structure round",
    "structure_follow_up_due": "a structure follow-up is due",
    "accretion_flagged": "the accretion ledger flagged guarded files",
}


def operating_card(
    *,
    workflow: dict[str, Any],
    action: str,
    next_command: str,
    lock_first: bool,
    open_threads: list[str],
    open_validation_gaps: list[str],
    flags: list[str],
    flagged_paths: list[str],
) -> dict[str, Any]:
    """Return the complete routine protocol for the action `inspect` recommends.

    `common_path_applies` is false exactly when `required_reading` is non-empty:
    the flags that name a reference are the states the card does not attempt to
    cover, and the agent reads that reference instead of following `sequence`.
    """
    obligations = OBLIGATIONS_BY_ACTION.get(action)
    if obligations is None:
        obligations = (
            TERMINAL_OBLIGATIONS
            if workflow["phase"] == "terminal"
            else IDLE_OBLIGATIONS
        )
    must = list(obligations["must"])
    if "accretion_flagged" in flags:
        must.append("acknowledge_structure_debt")
    steps = STEPS_BY_ACTION[action]
    sequence = [*LOCK_STEPS, *steps] if lock_first else list(steps)
    # Ordered by the flags themselves, which arrive most-blocking first, so the agent
    # reads the reference for its hardest stop before the rest.
    required_reading = list(
        dict.fromkeys(
            REFERENCE_BY_FLAG[flag] for flag in flags if flag in REFERENCE_BY_FLAG
        )
    )
    return {
        "phase": workflow["phase"],
        "primary_actor": workflow["primary_actor"],
        "allowed_events_by_actor": workflow["allowed_events_by_actor"],
        "action": action,
        "next_command": next_command,
        "sequence": sequence,
        "must": must,
        "must_not": list(obligations["must_not"]),
        "open_threads": open_threads,
        "open_validation_gaps": open_validation_gaps,
        "flags": flags,
        "accretion_flagged_paths": flagged_paths,
        "required_reading": required_reading,
        "common_path_applies": not required_reading,
    }


def render_card(card: dict[str, Any]) -> list[str]:
    """Render the card's identifiers as the sentences a human reader wants."""
    lines = [
        "operating_card:",
        f"  action: {card['action']}",
        f"  next: {card['next_command']}",
    ]
    if card["sequence"]:
        lines.append(
            "  then: "
            + "; ".join(SENTENCE_BY_STEP[step] for step in card["sequence"])
        )
    if card["must"]:
        lines.append(
            "  must: " + "; ".join(SENTENCE_BY_MUST[item] for item in card["must"])
        )
    if card["must_not"]:
        lines.append(
            "  never: "
            + "; ".join(SENTENCE_BY_MUST_NOT[item] for item in card["must_not"])
        )
    if card["flags"]:
        lines.append(
            "  because: "
            + "; ".join(SENTENCE_BY_FLAG[flag] for flag in card["flags"])
        )
    if card["required_reading"]:
        lines.append(
            "  read first: "
            + ", ".join(card["required_reading"])
            + " — the routine sequence above is not sufficient here"
        )
    return lines
