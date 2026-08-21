"""Render review conversations and the skim-first summary report."""

from __future__ import annotations

import re
from typing import Any

from review_notes import NOTE_MARKER

RESOLUTION_LABEL_BY_DECISION = {
    "applied": "**Fixed.**",
    "declined": "**Declined, independently verified.**",
}
VERIFYING_EVENT_KINDS = {"reviewer_update", "final_review", "source_update"}
TIMEOUT_OUTCOMES = {"owner_timeout", "reviewer_timeout", "initial_review_timeout"}


def evidence_summary(value: Any) -> str:
    """Summarize structured evidence without exposing its full payload."""
    if not isinstance(value, dict):
        return "Unavailable"
    return f"{value.get('basis', 'unknown')}: {value.get('sanitized_result', '')}"


def thread_conversations(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Project durable per-thread conversations from immutable event history."""
    conversations: dict[str, dict[str, Any]] = {}
    for event in document.get("history", []):
        if not isinstance(event, dict):
            continue
        for thread in [*event.get("threads", []), *event.get("new_threads", [])]:
            if isinstance(thread, dict):
                conversations[thread["id"]] = {
                    "thread": thread,
                    "status": "open",
                    "conversation": [],
                }
        action_groups = (
            event.get("replies", []),
            event.get("decisions", []),
            event.get("resolutions", []),
            event.get("thread_impacts", []),
        )
        for actions in action_groups:
            for action in actions:
                if not isinstance(action, dict):
                    continue
                thread_id = action.get("thread_id")
                if thread_id in conversations:
                    conversations[thread_id]["conversation"].append(
                        {
                            "event_id": event.get("event_id"),
                            "kind": event.get("kind"),
                            "occurred_at": event.get("occurred_at"),
                            "entry": action,
                        }
                    )
                    action_name = action.get("action")
                    if event.get("kind") == "final_review" or action_name == "resolve":
                        conversations[thread_id]["status"] = "resolved"
                    elif action_name == "reopen":
                        conversations[thread_id]["status"] = "open"
    return [
        conversations[key]
        for key in sorted(conversations, key=lambda value: int(value[1:]))
    ]


def render_conversations(document: dict[str, Any]) -> str:
    """Render the current durable thread conversations as Markdown."""
    lines = ["# Current Review Threads", ""]
    for item in thread_conversations(document):
        thread = item["thread"]
        lines.extend(
            [
                f"## {thread['id']} [{thread['priority']}] {thread['title']}",
                "",
                f"- Status: {item['status']}",
                f"- Required behavior: {thread['required_behavior']}",
                f"- Original evidence: {evidence_summary(thread['evidence'])}",
                "",
            ]
        )
        for entry in item["conversation"]:
            action = entry["entry"]
            label = action.get("decision") or action.get("action") or "resolved"
            lines.extend(
                [
                    f"### {entry['kind']} — {label}",
                    "",
                    action.get("message", ""),
                    "",
                ]
            )
    if len(lines) == 2:
        lines.append("No threads.")
    return "\n".join(lines).rstrip() + "\n"


# --- summary projection helpers ---


def escape_html(text: str) -> str:
    """Render history-derived angle brackets and ampersands inert."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_cell(text: Any) -> str:
    """Make history text safe inside one Markdown table cell.

    HTML is entity-escaped, pipes are escaped, single line breaks collapse to
    spaces, and paragraph breaks become renderer-owned `<br>`, so message
    content can never open a new row, heading, or HTML element.
    """
    if not isinstance(text, str):
        return ""
    paragraphs = [
        " ".join(paragraph.split())
        for paragraph in re.split(r"\n\s*\n", escape_html(text).strip())
        if paragraph.strip()
    ]
    return "<br>".join(paragraphs).replace("|", "\\|")


def flatten_inline(text: Any) -> str:
    """Collapse history text to one inert line for list items and headings.

    One line cannot start a new heading or table row, and entity escaping
    keeps embedded HTML from rendering as markup.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(escape_html(text).split())


def completed_rounds(history: list[dict[str, Any]]) -> int:
    """Count owner replies that were verified by a later reviewer event."""
    rounds = 0
    awaiting_verification = False
    for event in history:
        kind = event.get("kind")
        if kind == "owner_reply":
            awaiting_verification = True
        elif kind in VERIFYING_EVENT_KINDS and awaiting_verification:
            rounds += 1
            awaiting_verification = False
    return rounds


def event_date(event: dict[str, Any]) -> str:
    """Return the recorded calendar date, preserving the event's offset."""
    occurred_at = event.get("occurred_at", "")
    return occurred_at.split("T")[0] if isinstance(occurred_at, str) else ""


def marked_notes(event: dict[str, Any]) -> list[dict[str, str]]:
    """Lift `Note to user:` lines from every per-thread message in one event.

    Threads raised in the event carry their own optional message, so notes
    flagged at raise time surface alongside notes on replies and decisions.
    """
    notes: list[dict[str, str]] = []
    carriers: list[tuple[dict[str, Any], str]] = []
    for actions in (
        event.get("replies", []),
        event.get("decisions", []),
        event.get("resolutions", []),
        event.get("thread_impacts", []),
    ):
        for action in actions:
            if isinstance(action, dict):
                carriers.append((action, str(action.get("thread_id", ""))))
    for thread in [*event.get("threads", []), *event.get("new_threads", [])]:
        if isinstance(thread, dict):
            carriers.append((thread, str(thread.get("id", ""))))
    for carrier, thread_id in carriers:
        message = carrier.get("message")
        if not isinstance(message, str):
            continue
        for line in message.splitlines():
            stripped = line.strip()
            if stripped.startswith(NOTE_MARKER):
                notes.append(
                    {
                        "text": stripped[len(NOTE_MARKER):].strip(),
                        "source": str(thread_id or ""),
                    }
                )
    return notes


def gap_records(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collect every gap definition and resolution message by gap ID."""
    records: dict[str, dict[str, Any]] = {}
    for event in history:
        validation = event.get("validation")
        if isinstance(validation, dict):
            for gap in validation.get("gaps", []):
                if isinstance(gap, dict) and isinstance(gap.get("gap_id"), str):
                    records[gap["gap_id"]] = {"gap": gap, "resolution": None}
        for resolution in event.get("gap_resolutions", []):
            if isinstance(resolution, dict):
                record = records.get(resolution.get("gap_id"))
                if record is not None:
                    record["resolution"] = resolution.get("message", "")
                    record["disposition"] = resolution.get("disposition")
                    record["justification"] = resolution.get("justification")
    return records


def summary_notes(document: dict[str, Any]) -> list[dict[str, str]]:
    """Project the Notes-for-You items: marked notes plus structured signals.

    One event contributes at most one note per thread and text: an
    agent-marked note suppresses the automatic deferred/blocked duplicate for
    its thread, while distinct notes from the same event stay distinct.
    """
    state = document["state"]
    notes: list[dict[str, str]] = []
    for event in document.get("history", []):
        marked = marked_notes(event)
        seen_texts = set()
        for note in marked:
            key = (note["source"], note["text"])
            if key in seen_texts:
                continue
            seen_texts.add(key)
            notes.append(note)
        marked_normalized = {" ".join(note["text"].split()) for note in marked}
        for reply in event.get("replies", []):
            if not isinstance(reply, dict):
                continue
            if reply.get("decision") != "deferred/blocked":
                continue
            generated = (
                f"[blocked] {reply.get('blocker', '')} Remaining work: "
                f"{reply.get('remaining_work', '')}"
            )
            # The blocked-work alert always surfaces; only a marked note that
            # is an exact normalized duplicate of it may replace it, so an
            # unrelated note on the same thread cannot hide the blocker.
            if " ".join(generated.split()) in marked_normalized:
                continue
            notes.append(
                {"text": generated, "source": str(reply.get("thread_id", ""))}
            )
    for event in document.get("history", []):
        debt = event.get("structure_debt")
        if (
            isinstance(debt, dict)
            and debt.get("disposition") == "structure_deferred"
        ):
            paths = ", ".join(
                path for path in debt.get("flagged_paths", []) if isinstance(path, str)
            )
            notes.append(
                {
                    "text": (
                        f"[structure] Accretion-flagged files deferred without a "
                        f"structure round: {paths} — {debt.get('message', '')}"
                    ),
                    "source": "",
                }
            )
    terminal = state.get("terminal")
    if isinstance(terminal, dict) and terminal.get("outcome") in TIMEOUT_OUTCOMES:
        open_threads = ", ".join(state["threads"]["open"]) or "none"
        notes.append(
            {
                "text": (
                    f"[action required] Review ended by {terminal['outcome']} — "
                    f"threads still open at termination: {open_threads}"
                ),
                "source": "",
            }
        )
        records = gap_records(document.get("history", []))
        for gap_id in state["validation_gaps"]["open"]:
            record = records.get(gap_id)
            if record and record["gap"].get("material"):
                gap = record["gap"]
                notes.append(
                    {
                        "text": (
                            f"[action required] Material validation gap still open: "
                            f"{gap.get('check', '')} — {gap.get('reason', '')}"
                        ),
                        "source": gap_id,
                    }
                )
    return notes


def render_note_item(note: dict[str, str]) -> str:
    """Render one note as a list item, bolding a leading bracketed tag."""
    text = flatten_inline(note["text"])
    match = re.match(r"^\[([^\]]+)\]\s*(.*)$", text)
    if match:
        text = f"**[{match.group(1)}]** {match.group(2)}"
    suffix = f" *({note['source']})*" if note["source"] else ""
    return f"- {text}{suffix}"


def thread_sort_key(item: dict[str, Any]) -> tuple[str, int]:
    thread = item["thread"]
    return (thread.get("priority", "P3"), int(thread["id"][1:]))


def end_picture(item: dict[str, Any], workflow: dict[str, Any]) -> str:
    """Derive the End-picture cell from one thread's conversation trail."""
    conversation = item["conversation"]
    if item["status"] == "resolved":
        last_decision = None
        resolution_message = ""
        for entry in conversation:
            action = entry["entry"]
            if "decision" in action:
                last_decision = action["decision"]
            if entry["kind"] == "final_review" or action.get("action") == "resolve":
                resolution_message = action.get("message", "")
        label = RESOLUTION_LABEL_BY_DECISION.get(last_decision, "**Resolved.**")
        return f"{label} {resolution_message}".strip()
    awaiting = workflow.get("primary_actor") or "none"
    if conversation:
        action = conversation[-1]["entry"]
        label = action.get("decision") or action.get("action") or "update"
        base = f"{label}: {action.get('message', '')}"
    else:
        base = "open"
    return f"{base} — in progress, awaiting {awaiting}"


def rejection_cell(item: dict[str, Any]) -> str:
    """Collect declined/deferred reasons and post-applied reviewer pushback."""
    parts: list[str] = []
    last_owner_decision = None
    for entry in item["conversation"]:
        action = entry["entry"]
        if "decision" in action:
            if action["decision"] != "applied":
                parts.append(action.get("message", ""))
            last_owner_decision = action["decision"]
        elif (
            action.get("action") in {"comment", "reopen"}
            and last_owner_decision == "applied"
        ):
            parts.append(action.get("message", ""))
    return "; ".join(part for part in parts if part) or "—"


def render_header(document: dict[str, Any], note_count: int) -> list[str]:
    state = document["state"]
    workflow = state["workflow"]
    history = document.get("history", [])
    terminal = state.get("terminal")
    lines = [
        f"# Review Summary — {flatten_inline(document['name'])} "
        f"(`{document['review_id']}`)",
        "",
    ]
    if isinstance(terminal, dict):
        rounds = completed_rounds(history)
        rounds_text = f"{rounds} completed review round{'s' if rounds != 1 else ''}"
        events_text = f"{len(history)} event{'s' if len(history) != 1 else ''}"
        date = event_date(history[-1]) if history else ""
        outcome = terminal.get("outcome")
        outcome_text = (
            "**Outcome: LGTM**"
            if outcome == "lgtm"
            else f"**Outcome: ended by {outcome} — review incomplete**"
        )
        lines.append(f"- {outcome_text} · {rounds_text}, {events_text} · {date}")
    else:
        actor = workflow.get("primary_actor") or "none"
        action = (workflow.get("primary_action") or {}).get("kind", "none")
        open_threads = ", ".join(state["threads"]["open"]) or "none"
        lines.append(
            f"- **In progress** — waiting on: {actor} to {action}"
            f" · open: {open_threads}"
        )
    prior_review_id = document.get("prior_review_id")
    if prior_review_id:
        lines.append(f"- Prior review: `{prior_review_id}`")
    if document.get("review_kind") == "structure":
        lines.append("- Kind: structure round — behavior-preserving shape review")
    if note_count:
        lines.append(
            f"- **Attention: {note_count} note{'s' if note_count != 1 else ''}"
            " for you**"
        )
    else:
        lines.append("- No notes — nothing flagged for you")
    return lines


def render_issue_summary(document: dict[str, Any]) -> list[str]:
    workflow = document["state"]["workflow"]
    threads = sorted(thread_conversations(document), key=thread_sort_key)
    gaps = gap_records(document.get("history", []))
    lines = ["## Issue Summary", ""]
    if not threads and not gaps:
        lines.append("No findings raised.")
        return lines
    lines.extend(
        [
            "| ID | Raised issue | End picture | Rejected / deferred (why) |",
            "|---|---|---|---|",
        ]
    )
    for item in threads:
        thread = item["thread"]
        identity = f"{thread['id']} `{thread.get('priority', '')}`"
        raised = escape_cell(
            f"{thread.get('title', '')} — {thread.get('risk', '')}"
        )
        lines.append(
            f"| {identity} | {raised} | {escape_cell(end_picture(item, workflow))} |"
            f" {escape_cell(rejection_cell(item))} |"
        )
    open_gaps = set(document["state"]["validation_gaps"]["open"])
    for gap_id in sorted(gaps, key=lambda value: int(value[1:])):
        record = gaps[gap_id]
        gap = record["gap"]
        badge = "material" if gap.get("material") else "non-material"
        raised = escape_cell(f"{gap.get('check', '')} — {gap.get('reason', '')}")
        if gap_id in open_gaps:
            picture = "open — awaiting reviewer resolution"
        elif record.get("disposition") == "unavailable_non_material":
            # The reader must never infer that an unavailable check was performed, and
            # every claim here is quoted from the validated event rather than asserted.
            justification = record.get("justification") or {}
            picture = " ".join(
                part
                for part in (
                    "**Resolved without performing the check.**",
                    f"Not performed: {justification.get('unperformed_check', '')}."
                    if justification.get("unperformed_check")
                    else "",
                    f"Fails closed: {justification.get('fail_closed_behavior', '')}."
                    if justification.get("fail_closed_behavior")
                    else "",
                    record["resolution"] or "",
                )
                if part
            ).strip()
        else:
            picture = f"**Resolved.** {record['resolution'] or ''}".strip()
        lines.append(
            f"| {gap_id} `{badge}` | {raised} | {escape_cell(picture)} | — |"
        )
    return lines


def render_verification(document: dict[str, Any]) -> list[str]:
    state = document["state"]
    history = document.get("history", [])
    lines = ["## Verification", ""]
    check_groups = []
    for event in history:
        validation = event.get("validation")
        if isinstance(validation, dict) and validation.get("performed"):
            check_groups.append(validation["performed"])
    if check_groups:
        # The latest validating event carries the final verification state;
        # earlier rounds roll up into one line, except failures, which always
        # stay visible individually.
        for check in check_groups[-1]:
            lines.append(
                f"- {check.get('result')}: {flatten_inline(check.get('check'))}"
            )
        earlier = [check for group in check_groups[:-1] for check in group]
        for check in earlier:
            if check.get("result") == "failed":
                lines.append(
                    f"- failed (earlier round): {flatten_inline(check.get('check'))}"
                )
        passed_earlier = sum(
            1 for check in earlier if check.get("result") != "failed"
        )
        if passed_earlier:
            lines.append(
                f"- Earlier rounds recorded {passed_earlier} more passed"
                f" check{'s' if passed_earlier != 1 else ''};"
                " see `threads` or canonical JSON"
            )
    else:
        lines.append("- No validation checks recorded")
    fingerprint = state.get("source_fingerprint")
    if fingerprint and history:
        # Timeout events carry no snapshot, so walk history backward for the
        # snapshot that recorded the current guarded fingerprint.
        snapshot: dict[str, Any] | None = None
        for event in reversed(history):
            for key in ("completed_source_snapshot", "source_snapshot"):
                candidate = event.get(key)
                if (
                    isinstance(candidate, dict)
                    and candidate.get("fingerprint") == fingerprint
                ):
                    snapshot = candidate
                    break
            if snapshot is not None:
                break
        scope = snapshot.get("scope", []) if isinstance(snapshot, dict) else []
        scope_text = ", ".join(f"`{path}`" for path in scope) or "unrecorded scope"
        count = f"{len(scope)} file{'s' if len(scope) != 1 else ''}"
        terminal = state.get("terminal")
        subject = (
            "LGTM applies to"
            if isinstance(terminal, dict) and terminal.get("outcome") == "lgtm"
            else "Recorded source"
        )
        lines.append(
            f"- {subject} fingerprint `{fingerprint[:8]}…` over {count}: {scope_text}"
        )
    lines.extend(
        [
            "- Approval freshness: run `inspect` — this page is a cache and does"
            " not know current drift",
            "- Full conversations: `threads` command or canonical JSON; this page"
            " is intentionally a skim view",
        ]
    )
    return lines


def render_report(document: dict[str, Any]) -> str:
    """Render the skim-first summary page from canonical history."""
    notes = summary_notes(document)
    lines = render_header(document, len(notes))
    lines.extend(["", "## Notes for You", ""])
    if notes:
        lines.extend(render_note_item(note) for note in notes)
    else:
        lines.append("None recorded — neither agent flagged a design-shifting change")
    lines.append("")
    lines.extend(render_issue_summary(document))
    lines.append("")
    lines.extend(render_verification(document))
    return "\n".join(lines).rstrip() + "\n"
