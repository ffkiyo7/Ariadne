"""Bounded previous-round evidence for a retried Hermes TASK.

Without feedback there is no loop, only repeated invocation.  This module
collects the deterministic evidence of the previous round - the terminal
error, failed verification output, the worker's own clarification, and the
owner's revision feedback - and renders it as one bounded prompt section.

Scope rules, applied deliberately:

- Only evidence *newer than the previous TASK approval* is included, so a
  third attempt is not haunted by first-round noise that was already fixed.
- Evidence is deterministic facts plus owner words.  Raw reviewer prose is
  excluded on purpose: the owner's revision feedback and the revised TASK
  are the curated channel for review conclusions.
"""

from __future__ import annotations

import json

from .models import TurnKind, TurnState
from .state import StateStore

_ITEM_LIMIT = 700
_TOTAL_LIMIT = 3000

_EVIDENCE_ACTIONS = (
    "task-verification-failed",
    "review-failed",
    "owner-return-for-revision",
    "owner-reject",
)


def _bounded(text: str, limit: int = _ITEM_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _verification_failures(details_json: str) -> str | None:
    try:
        details = json.loads(details_json)
    except (TypeError, ValueError):
        return None
    failures = []
    for entry in details.get("verification", []):
        if not isinstance(entry, dict) or entry.get("passed"):
            continue
        command = " ".join(entry.get("command", [])) or "unknown command"
        summary = str(entry.get("summary", "")).strip()
        failures.append(f"`{command}` failed" + (f":\n{summary}" if summary else ""))
    return "\n".join(failures) or None


def _audit_feedback(details_json: str, key: str) -> str | None:
    try:
        details = json.loads(details_json)
    except (TypeError, ValueError):
        return None
    value = str(details.get(key, "")).strip()
    return value or None


def build_retry_context(state: StateStore, *, harness_session_id: str) -> str | None:
    """Return the previous-round evidence section body, or None on round one."""

    approvals = state.list_session_audits(
        harness_session_id, ("task-approved-hermes-queued",)
    )
    if len(approvals) < 2:
        # The current approval is already recorded when the Hermes turn runs,
        # so fewer than two approvals means there was no previous round.
        return None
    previous_approval_id = approvals[-2].id

    items: list[tuple[int, str, str]] = []

    turns = state.list_turns(harness_session_id)
    failed_hermes = [
        turn
        for turn in turns
        if turn.execution_kind is TurnKind.HERMES
        and turn.state in {TurnState.FAILED, TurnState.INTERRUPTED}
        and turn.error_summary
    ]
    if failed_hermes:
        last = failed_hermes[-1]
        items.append((0, "上一次尝试的终止原因", last.error_summary or ""))

    clarification = state.latest_clarification(harness_session_id)
    # ISO-8601 UTC strings compare chronologically; skip clarifications that
    # predate the previous approval - they were answered by an earlier revision.
    if clarification is not None and clarification.created_at >= approvals[-2].created_at:
        items.append(
            (
                1,
                "你上一轮提出的澄清请求（当前 TASK 已按 owner 决定修订）",
                f"Blocker: {clarification.blocker}\nRecommendation: {clarification.recommendation}",
            )
        )

    for fact in state.list_session_audits(harness_session_id, _EVIDENCE_ACTIONS):
        if fact.id <= previous_approval_id:
            continue
        if fact.action == "task-verification-failed":
            body = _verification_failures(fact.details_json)
            title = "上一轮验证命令失败"
        elif fact.action == "review-failed":
            body = _audit_feedback(fact.details_json, "summary")
            title = "上一轮 review 未通过"
        else:
            body = _audit_feedback(fact.details_json, "feedback")
            title = "owner 的修订反馈"
        if body:
            items.append((fact.id, title, body))

    if not items:
        return None

    sections = []
    total = 0
    for _, title, body in items:
        rendered = f"### {title}\n{_bounded(body)}"
        if total + len(rendered) > _TOTAL_LIMIT:
            break
        sections.append(rendered)
        total += len(rendered)
    return "\n\n".join(sections) or None
