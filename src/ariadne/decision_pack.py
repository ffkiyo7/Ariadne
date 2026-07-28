"""Owner decision pack: model narrative first, deterministic facts as audit.

The owner accepts work by reading what the models say about it - the
implementer's commit self-report and the reviewer's structured conclusion.
Deterministic Git/verification facts are attached not as the main content
but as a lie detector: every checkable claim in the narrative is compared
against the recorded facts, and any mismatch is surfaced as a red flag.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import Provider, TurnKind, TurnState
from .profile import ProjectProfile
from .runner import build_child_environment
from .state import StateStore


class DecisionPackError(RuntimeError):
    pass


_NARRATIVE_LIMIT = 3500

_REVIEW_SECTION_ALIASES = {
    "verdict": {"结论", "verdict", "conclusion"},
    "change_audit": {"改动核对", "change audit", "what the diff does", "改动自述核对"},
    "behavior": {"行为变化", "behavior changes", "behaviour changes"},
    "risks": {"风险与遗留", "风险", "risks", "risks and leftovers"},
    "sufficiency": {"task 充分性", "task充分性", "task sufficiency", "任务充分性"},
}

_REVIEW_SECTION_TITLES = {
    "verdict": "结论",
    "change_audit": "改动核对",
    "behavior": "行为变化",
    "risks": "风险与遗留",
    "sufficiency": "TASK 充分性",
}


@dataclass(frozen=True)
class DecisionPack:
    harness_session_id: str
    hermes_turn_id: str
    review_turn_id: str
    head_sha: str
    implementer_subject: str
    implementer_body: str
    claimed_files: tuple[str, ...]
    reviewer_sections: tuple[tuple[str, str], ...]
    reviewer_verdict: str | None
    committed: tuple[tuple[str, str], ...]
    shortstat: str
    verification: tuple[tuple[str, bool], ...]
    knowledge_updated: bool
    mismatches: tuple[str, ...]


def _git(worktree: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=build_child_environment(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise DecisionPackError("could not read Git facts for the decision pack") from exc
    return result.stdout.strip()


def parse_commit_report(message: str) -> tuple[str, str, tuple[str, ...], bool]:
    """Split a commit message into subject, narrative body, and claimed files.

    Returns (subject, body, claimed_files, files_section_present).
    """

    lines = message.splitlines()
    subject = lines[0].strip() if lines else ""
    body_lines: list[str] = []
    claimed: list[str] = []
    in_files = False
    files_present = False
    for line in lines[1:]:
        stripped = line.strip()
        if re.fullmatch(r"files\s*:", stripped, re.IGNORECASE):
            in_files = True
            files_present = True
            continue
        if in_files:
            item = stripped.lstrip("-* ").strip().strip("`")
            if item:
                claimed.append(item.replace("\\", "/"))
            elif stripped:
                in_files = False
                body_lines.append(line)
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return subject, body, tuple(dict.fromkeys(claimed)), files_present


def _payload_lines(sanitized_path: Path) -> list[dict]:
    payloads: list[dict] = []
    try:
        with sanitized_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict) or record.get("stream") != "stdout":
                    continue
                line = record.get("line")
                if not isinstance(line, str):
                    continue
                try:
                    payload = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    payloads.append(payload)
    except OSError:
        return []
    return payloads


def extract_final_narrative(sanitized_path: Path, provider: Provider) -> str | None:
    """Pull the reviewer's final message out of its redacted transcript."""

    final: str | None = None
    for payload in _payload_lines(sanitized_path):
        if provider is Provider.CLAUDE:
            if payload.get("type") == "result" and isinstance(payload.get("result"), str):
                text = payload["result"].strip()
                if text:
                    final = text
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        if str(item.get("type") or "") in {"agent_message", "assistant_message", "message"}:
            text = item.get("text") or item.get("message") or item.get("content")
            if isinstance(text, str) and text.strip():
                final = text.strip()
    return final[:_NARRATIVE_LIMIT] if final else None


def split_review_sections(narrative: str) -> tuple[tuple[tuple[str, str], ...], str | None]:
    """Split the reviewer narrative into known sections and extract a verdict."""

    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in narrative.splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            key = heading.group(1).strip().rstrip(":").casefold()
            current = next(
                (
                    name
                    for name, aliases in _REVIEW_SECTION_ALIASES.items()
                    if key in {alias.casefold() for alias in aliases}
                ),
                None,
            )
            if current:
                found.setdefault(current, [])
            continue
        if current:
            found[current].append(line)
    sections = tuple(
        (_REVIEW_SECTION_TITLES[name], "\n".join(body).strip())
        for name, body in found.items()
        if "\n".join(body).strip()
    )
    if not sections:
        sections = (("结论", narrative.strip()),)
    verdict: str | None = None
    verdict_text = dict(sections).get("结论", "").casefold()
    if re.search(r"\bfail\b|不通过|未通过", verdict_text):
        verdict = "FAIL"
    elif re.search(r"\bpass\b|通过", verdict_text):
        verdict = "PASS"
    return sections, verdict


def _verification_from_audit(state: StateStore, harness_session_id: str) -> tuple[tuple[str, bool], ...]:
    facts = state.list_session_audits(harness_session_id, ("task-verified",))
    if not facts:
        return ()
    try:
        details = json.loads(facts[-1].details_json)
    except (TypeError, ValueError):
        return ()
    results = []
    for entry in details.get("verification", []):
        if isinstance(entry, dict) and isinstance(entry.get("command"), list):
            results.append((" ".join(entry["command"]), bool(entry.get("passed"))))
    return tuple(results)


def build_decision_pack(
    state: StateStore,
    *,
    harness_session_id: str,
    profile: ProjectProfile,
) -> DecisionPack:
    session = state.get_session(harness_session_id)
    pipeline = state.get_pipeline_run(harness_session_id)
    turns = state.list_turns(harness_session_id)
    hermes_turn = next((turn for turn in turns if turn.id == pipeline.task_turn_id), None)
    review_turn = next(
        (
            turn
            for turn in reversed(turns)
            if turn.execution_kind is TurnKind.REVIEW and turn.state is TurnState.SUCCEEDED
        ),
        None,
    )
    if hermes_turn is None or review_turn is None:
        raise DecisionPackError("decision pack requires a completed Hermes turn and review turn")
    start_sha = pipeline.task_start_head_sha
    if not start_sha:
        raise DecisionPackError("decision pack requires the recorded TASK start head SHA")

    head_sha = _git(session.worktree, "rev-parse", "HEAD")
    commit_message = _git(session.worktree, "log", "-1", "--format=%B", head_sha)
    shortstat = _git(session.worktree, "diff", "--shortstat", f"{start_sha}..{head_sha}")
    name_status = _git(session.worktree, "diff", "--name-status", f"{start_sha}..{head_sha}")
    committed: list[tuple[str, str]] = []
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            committed.append((parts[0].strip(), parts[-1].strip()))

    subject, body, claimed, files_present = parse_commit_report(commit_message)

    reviewer_sections: tuple[tuple[str, str], ...] = ()
    verdict: str | None = None
    if review_turn.sanitized_path is not None:
        provider = state.get_provider_session(review_turn.provider_session_id).provider
        narrative = extract_final_narrative(review_turn.sanitized_path, provider)
        if narrative:
            reviewer_sections, verdict = split_review_sections(narrative)

    actual_paths = {path for _, path in committed}
    knowledge_path = profile.knowledge_file.as_posix() if profile.knowledge_file else None

    mismatches: list[str] = []
    if not files_present:
        mismatches.append("实现自述缺少 Files 清单：文件级声明无法核对")
    else:
        claimed_set = set(claimed)
        phantom = sorted(claimed_set - actual_paths)
        undeclared = sorted(actual_paths - claimed_set)
        if phantom:
            mismatches.append("自述声称改动但 Git 中不存在：" + ", ".join(phantom))
        if undeclared:
            mismatches.append("Git 中实际改动但自述未提及：" + ", ".join(undeclared))
    if not reviewer_sections:
        mismatches.append("未能从 review 转录提取结构化结论；请回看原始转录")

    return DecisionPack(
        harness_session_id=harness_session_id,
        hermes_turn_id=hermes_turn.id,
        review_turn_id=review_turn.id,
        head_sha=head_sha,
        implementer_subject=subject,
        implementer_body=body,
        claimed_files=claimed,
        reviewer_sections=reviewer_sections,
        reviewer_verdict=verdict,
        committed=tuple(committed),
        shortstat=shortstat,
        verification=_verification_from_audit(state, harness_session_id),
        knowledge_updated=bool(knowledge_path and knowledge_path in actual_paths),
        mismatches=tuple(mismatches),
    )


def decision_pack_detail_text(pack: DecisionPack) -> str:
    """Full deterministic facts for the on-demand detail view."""

    lines = [f"## {pack.harness_session_id} 决策包详情", ""]
    lines.append(f"**head SHA**（`!accept` 使用）:")
    lines.append(f"```\n{pack.head_sha}\n```")
    lines.append(f"**diffstat:** {pack.shortstat or '无'}")
    lines.append("**改动文件（Git 事实）:**")
    lines.extend(f"- `{status}` `{path}`" for status, path in pack.committed)
    if pack.verification:
        lines.append("**验证命令:**")
        lines.extend(
            f"- {'✅' if passed else '❌'} `{command}`" for command, passed in pack.verification
        )
    lines.append(
        f"**turns:** Hermes `{pack.hermes_turn_id}` · Review `{pack.review_turn_id}`"
    )
    if pack.knowledge_updated:
        lines.append("**知识库:** 本次提交包含项目知识更新")
    if pack.mismatches:
        lines.append("**❗ 自述与事实不一致:**")
        lines.extend(f"- {item}" for item in pack.mismatches)
    return "\n".join(lines)
