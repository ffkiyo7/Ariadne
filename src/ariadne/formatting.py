"""Bounded status-card formatting for Discord."""

from __future__ import annotations

from dataclasses import dataclass

from .models import HarnessSession, ProviderSession, Turn
from .redaction import Redactor


@dataclass(frozen=True)
class StatusCard:
    title: str
    description: str
    fields: tuple[tuple[str, str], ...]


def chunk_message(text: str, limit: int = 1900) -> list[str]:
    """Split long text on line boundaries instead of hard character cuts.

    A hard cut at position N breaks sentences mid-word and produces the
    "full-width then broken" rendering in Discord.  Prefer the last newline
    before the limit; fall back to a hard cut only for a single line longer
    than the limit.
    """

    if limit < 1:
        raise ValueError("chunk limit must be positive")
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return [chunk for chunk in chunks if chunk.strip()]


def build_status_card(
    *,
    session: HarnessSession,
    provider: ProviderSession | None,
    turn: Turn | None,
    queue_position: int | None,
    error_summary: str | None = None,
    redactor: Redactor | None = None,
    links: tuple[tuple[str, str], ...] = (),
) -> StatusCard:
    redactor = redactor or Redactor()
    provider_name = provider.provider.value if provider else "not-started"
    configuration = "locked" if provider and provider.configuration_locked else "awaiting selection"
    requested = turn.requested_model if turn else "-"
    configured = turn.configured_model if turn else (provider.default_model if provider else "-")
    requested_effort = turn.requested_effort if turn else "-"
    configured_effort = turn.configured_effort if turn else (provider.default_effort if provider else "-")
    reported = (turn.reported_model if turn and turn.reported_model else "not reported") if turn else "not reported"
    turn_id = turn.id if turn else "-"
    executor = turn.execution_kind.value if turn else "-"
    state = turn.state.value if turn else session.status.value
    queue = "running" if queue_position == 0 else (str(queue_position) if queue_position is not None else "-")
    safe_error = redactor.redact(error_summary or "")[:500] or "-"
    next_step = {
        "draft": "强模型 turn 已入队，等待 runner。",
        "queued": "等待唯一 runner slot。",
        "launching": "transient unit 正在启动。",
        "running": "等待 provider 完成；可用 !stop。",
        "waiting_for_owner": "等待 owner 的 PLAN/TASK/验收动作。",
        "plan_approved": "请在置顶状态卡中批准一个受控 TASK，才会交给 Hermes。",
        "task_running": "受控 Hermes turn 正在运行；可用 !stop。",
        "review_pending": "TASK 已完成，等待强模型 review 与 owner 确认。",
        "pr_open": "Draft PR 已创建，等待 CI 事实检查。",
        "ci_passed": "CI 已通过，等待 profile 要求的 preview 验证。",
        "preview_ready": "Preview 已通过；只有 owner 的 !accept 才能合并。",
        "needs_owner": "受控执行或 review 需要 owner 决定；可重新批准同一或修订后的 TASK。",
        "failed": "检查安全错误摘要后使用 !resume 或修正配置。",
        "interrupted": "确认 worktree 后使用 !resume。",
    }.get(state, "按状态卡和 owner 门禁继续。")
    if provider and not provider.configuration_locked and turn is None:
        next_step = "请使用此置顶卡选择并固定配置；固定前不会创建或运行模型 turn。"
    # Jump links keep the owner's attention anchored: the pinned card always
    # points at the newest decision-relevant message instead of forcing a
    # scroll through the thread history.
    link_fields = tuple((name, url) for name, url in links if name and url)
    return StatusCard(
        title=f"{session.id} · {provider_name}",
        description=redactor.redact(next_step)[:1000],
        fields=(
            ("provider", provider_name),
            ("configuration", configuration),
            ("requested model", redactor.redact(requested)),
            ("configured model", redactor.redact(configured)),
            ("requested effort", redactor.redact(requested_effort)),
            ("configured effort", redactor.redact(configured_effort)),
            ("reported model", redactor.redact(reported)),
            ("branch", redactor.redact(session.branch)),
            ("queue position", queue),
            ("turn", turn_id),
            ("executor", executor),
            ("status", state),
            ("last safe error", safe_error),
        )
        + link_fields,
    )


def status_card_text(card: StatusCard) -> str:
    rows = [f"## {card.title}", card.description, ""]
    rows.extend(f"**{name}:** {value}" for name, value in card.fields)
    return "\n".join(rows)
