"""Render review conversations and the skim-first summary report."""

from __future__ import annotations

import re
from typing import Any

from review_schema import operations_of

RESOLUTION_LABEL_BY_DECISION = {
    "applied": "**Fixed.**",
    "declined": "**Declined, independently verified.**",
}
VERIFYING_EVENT_KINDS = {"reviewer_update", "final_review", "source_update"}
TIMEOUT_OUTCOMES = {"owner_timeout", "reviewer_timeout", "initial_review_timeout"}
# Operations that continue an existing thread's conversation, and the label each
# contributes to a rendered entry. `thread.reply` carries its own decision instead.
THREAD_ENTRY_LABELS = {
    "thread.comment": "comment",
    "thread.resolve": "resolve",
    "thread.reopen": "reopen",
}


def evidence_summary(value: Any) -> str:
    """Summarize structured evidence without exposing its full payload."""
    if not isinstance(value, dict):
        return "Unavailable"
    return f"{value.get('basis', 'unknown')}: {value.get('sanitized_result', '')}"


def entry_label(operation: dict[str, Any]) -> str:
    """Name the act one conversation entry records."""
    if operation.get("op") == "thread.reply":
        return str(operation.get("decision") or "reply")
    return THREAD_ENTRY_LABELS.get(operation.get("op"), "update")


def thread_conversations(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Project durable per-thread conversations from immutable event history."""
    conversations: dict[str, dict[str, Any]] = {}
    for event in document.get("history", []):
        for operation in operations_of(event):
            name = operation.get("op") if isinstance(operation, dict) else None
            if not isinstance(name, str):
                continue
            if name == "thread.open":
                thread_id = operation.get("id")
                if isinstance(thread_id, str):
                    conversations[thread_id] = {
                        "thread": operation,
                        "status": "open",
                        "conversation": [],
                    }
                continue
            if name not in THREAD_ENTRY_LABELS and name != "thread.reply":
                continue
            thread_id = operation.get("thread_id")
            if not isinstance(thread_id, str) or thread_id not in conversations:
                continue
            conversations[thread_id]["conversation"].append(
                {
                    "event_id": event.get("event_id"),
                    "kind": event.get("kind"),
                    "occurred_at": event.get("occurred_at"),
                    "entry": operation,
                }
            )
            if name == "thread.resolve":
                conversations[thread_id]["status"] = "resolved"
            elif name == "thread.reopen":
                conversations[thread_id]["status"] = "open"
    return [
        conversations[key]
        for key in sorted(conversations, key=lambda value: int(value[1:]))
    ]


def thread_summaries(
    document: dict[str, Any], open_only: bool = False
) -> list[dict[str, Any]]:
    """Project each thread's routing fields without its bodies or evidence.

    Deciding whether an agent may act on a thread needs identity, priority, status,
    title, and the files it concerns; `risk`, `required_behavior`, and every reply are
    dropped rather than shortened, so nothing returned here is a summary of prose.
    """
    summaries = []
    for item in thread_conversations(document):
        if open_only and item["status"] != "open":
            continue
        thread = item["thread"]
        summaries.append(
            {
                "id": thread["id"],
                "priority": thread["priority"],
                "status": item["status"],
                "title": thread["title"],
                "paths": list(thread.get("paths") or []),
            }
        )
    return summaries


def render_summaries(document: dict[str, Any], open_only: bool = False) -> str:
    """Render the routing summary of the current threads as Markdown."""
    heading = "Open Review Threads" if open_only else "Review Thread Summary"
    lines = [f"# {heading}", ""]
    for summary in thread_summaries(document, open_only):
        paths = ", ".join(summary["paths"]) or "none"
        lines.append(
            f"- {summary['id']} [{summary['priority']}] {summary['status']}: "
            f"{summary['title']} ({paths})"
        )
    if len(lines) == 2:
        lines.append("No threads.")
    return "\n".join(lines).rstrip() + "\n"


def render_conversations(document: dict[str, Any], open_only: bool = False) -> str:
    """Render the current durable thread conversations as Markdown."""
    lines = ["# Current Review Threads", ""]
    for item in thread_conversations(document):
        if open_only and item["status"] != "open":
            continue
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
            lines.extend(
                [
                    f"### {entry['kind']} — {entry_label(action)}",
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


def attached_notes(event: dict[str, Any]) -> list[dict[str, str]]:
    """Collect the typed notes one transaction recorded for the user.

    A note is a `note.attach` record rather than a marker parsed out of prose,
    so editing a message body can neither add nor remove one.
    """
    notes: list[dict[str, str]] = []
    for operation in operations_of(event):
        if not isinstance(operation, dict) or operation.get("op") != "note.attach":
            continue
        target = operation.get("target")
        notes.append(
            {
                "text": f"[{operation.get('tag', '')}] {operation.get('message', '')}",
                "source": str(target.get("id", "")) if isinstance(target, dict) else "",
            }
        )
    return notes


def gap_records(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collect every gap definition and resolution message by gap ID."""
    records: dict[str, dict[str, Any]] = {}
    for event in history:
        for operation in operations_of(event):
            if not isinstance(operation, dict):
                continue
            name = operation.get("op")
            if name == "gap.open" and isinstance(operation.get("gap_id"), str):
                records[operation["gap_id"]] = {"gap": operation, "resolution": None}
            elif name == "gap.resolve":
                gap_id = operation.get("gap_id")
                record = records.get(gap_id) if isinstance(gap_id, str) else None
                if record is not None:
                    record["resolution"] = operation.get("message", "")
                    record["disposition"] = operation.get("disposition")
                    record["justification"] = operation.get("justification")
    return records


def summary_notes(document: dict[str, Any]) -> list[dict[str, str]]:
    """Project the Notes-for-You items: attached notes plus structured signals.

    One transaction contributes at most one note per target and text, so a
    repeated `note.attach` does not read as two findings.
    """
    state = document["state"]
    notes: list[dict[str, str]] = []
    for event in document.get("history", []):
        seen_texts = set()
        for note in attached_notes(event):
            key = (note["source"], note["text"])
            if key in seen_texts:
                continue
            seen_texts.add(key)
            notes.append(note)
        for operation in operations_of(event):
            if not isinstance(operation, dict):
                continue
            if operation.get("op") != "thread.reply":
                continue
            if operation.get("decision") != "deferred/blocked":
                continue
            notes.append(
                {
                    "text": (
                        f"[blocked] {operation.get('blocker', '')} Remaining work: "
                        f"{operation.get('remaining_work', '')}"
                    ),
                    "source": str(operation.get("thread_id", "")),
                }
            )
    for event in document.get("history", []):
        for operation in operations_of(event):
            if not isinstance(operation, dict):
                continue
            debt = (
                operation.get("structure_debt")
                if operation.get("op") == "review.approve"
                else None
            )
            if (
                not isinstance(debt, dict)
                or debt.get("disposition") != "structure_deferred"
            ):
                continue
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
            if action.get("op") == "thread.reply":
                last_decision = action.get("decision")
            elif action.get("op") == "thread.resolve":
                resolution_message = action.get("message", "")
        label = (
            RESOLUTION_LABEL_BY_DECISION.get(last_decision, "**Resolved.**")
            if isinstance(last_decision, str)
            else "**Resolved.**"
        )
        return f"{label} {resolution_message}".strip()
    awaiting = workflow.get("primary_actor") or "none"
    if conversation:
        action = conversation[-1]["entry"]
        base = f"{entry_label(action)}: {action.get('message', '')}"
    else:
        base = "open"
    return f"{base} — in progress, awaiting {awaiting}"


def rejection_cell(item: dict[str, Any]) -> str:
    """Collect declined/deferred reasons and post-applied reviewer pushback."""
    parts: list[str] = []
    last_owner_decision = None
    for entry in item["conversation"]:
        action = entry["entry"]
        if action.get("op") == "thread.reply":
            if action.get("decision") != "applied":
                parts.append(action.get("message", ""))
            last_owner_decision = action.get("decision")
        elif (
            action.get("op") in {"thread.comment", "thread.reopen"}
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
        (
            f"# Review Summary — {flatten_inline(document['name'])} "
            f"(`{document['review_id']}`)"
        ),
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
        performed = [
            operation
            for operation in operations_of(event)
            if isinstance(operation, dict) and operation.get("op") == "check.record"
        ]
        if performed:
            check_groups.append(performed)
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
            (
                "- Approval freshness: run `inspect` — this page is a cache "
                "and does not know current drift"
            ),
            (
                "- Full conversations: `threads` command or canonical JSON; "
                "this page is intentionally a skim view"
            ),
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
