"""Render review conversations and latest-event reports."""

from __future__ import annotations

from typing import Any


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


def render_report(document: dict[str, Any]) -> str:
    """Render the latest canonical event and projected state as Markdown."""
    state = document["state"]
    workflow = state["workflow"]
    lines = [
        "# Latest Review Report",
        "",
        f"- Review ID: {document['review_id']}",
        f"- Review name: {document['name']}",
        f"- Workflow phase: {workflow['phase']}",
        f"- Primary actor: {workflow['primary_actor'] or 'None'}",
        f"- Primary action: {(workflow['primary_action'] or {}).get('kind', 'None')}",
        f"- Open threads: {', '.join(state['threads']['open']) or 'None'}",
        f"- Resolved threads: {', '.join(state['threads']['resolved']) or 'None'}",
        f"- Open validation gaps: {', '.join(state['validation_gaps']['open']) or 'None'}",
        f"- Resolved validation gaps: {', '.join(state['validation_gaps']['resolved']) or 'None'}",
        f"- Source fingerprint: {state['source_fingerprint'] or 'Unavailable'}",
    ]
    if not document["history"]:
        return "\n".join(lines) + "\n"
    event = document["history"][-1]
    lines.extend(
        [
            f"- Latest event: {event['kind']} ({event['event_id']})",
            f"- Recorded at: {event['occurred_at']}",
            "",
        ]
    )
    threads = event.get("threads", []) or event.get("new_threads", [])
    if threads:
        lines.extend(["## Threads", ""])
        for thread in threads:
            lines.extend(
                [
                    f"### {thread['id']} [{thread['priority']}]: {thread['title']}",
                    "",
                    thread["risk"],
                    "",
                    f"Evidence: {evidence_summary(thread['evidence'])}",
                    "",
                    f"Required behavior: {thread['required_behavior']}",
                    "",
                ]
            )
    actions = (
        event.get("replies")
        or event.get("decisions")
        or event.get("resolutions")
        or event.get("thread_impacts")
        or []
    )
    if actions:
        lines.extend(["## Thread Actions", ""])
        for action in actions:
            label = action.get("decision") or action.get("action") or "resolved"
            lines.extend(
                [f"### {action['thread_id']}: {label}", "", action["message"], ""]
            )
    validation = event.get("validation")
    if isinstance(validation, dict):
        lines.extend(["## Validation", ""])
        for check in validation.get("performed", []):
            lines.append(f"- {check.get('result')}: {check.get('check')}")
        if not validation.get("performed"):
            lines.append("- None")
        lines.extend(["", "## Validation Gaps", ""])
        for gap in validation.get("gaps", []):
            material = "material" if gap.get("material") else "non-material"
            lines.append(f"- [{material}] {gap.get('check')}: {gap.get('reason')}")
        if not validation.get("gaps"):
            lines.append("- None")
    if state["terminal"]:
        lines.extend(
            [
                "",
                "## Terminal Outcome",
                "",
                f"- Outcome: {state['terminal']['outcome']}",
                f"- Occurred at: {state['terminal']['occurred_at']}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
