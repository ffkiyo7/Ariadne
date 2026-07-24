"""Hermes NEEDS_CLARIFICATION protocol.

A worker turn has exactly two terminal outcomes: one implementation commit,
or one `ARIADNE-CLARIFICATION.md` file at the worktree root with no other
change.  This module owns the file contract; detection and enforcement live
in the runner's completion gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


CLARIFICATION_FILENAME = "ARIADNE-CLARIFICATION.md"

_SECTION_LIMIT = 4000


class InvalidClarification(ValueError):
    pass


@dataclass(frozen=True)
class ClarificationRequest:
    blocker: str
    insufficiency: str
    options: str
    recommendation: str
    impact: str


_ALIASES = {
    "blocker": {"blocker", "阻塞", "阻塞点"},
    "insufficiency": {
        "why the task is insufficient",
        "insufficiency",
        "task 缺陷",
        "任务缺陷",
        "task不足",
    },
    "options": {"options", "选项", "可选方案"},
    "recommendation": {"recommendation", "建议", "推荐方案"},
    "impact": {"why / impact", "why/impact", "why impact", "impact", "影响", "理由与影响"},
}


def _heading_key(text: str) -> str:
    text = re.sub(r"^\s*#+\s*", "", text).strip().rstrip(":")
    return text.casefold()


def _sections(text: str) -> dict[str, str]:
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if re.match(r"^\s*#{1,6}\s+", line):
            key = _heading_key(line)
            current = next(
                (
                    name
                    for name, aliases in _ALIASES.items()
                    if key in {alias.casefold() for alias in aliases}
                ),
                None,
            )
            if current:
                found.setdefault(current, [])
            continue
        if current:
            found[current].append(line)
    return {key: "\n".join(value).strip() for key, value in found.items()}


def parse_clarification(text: str) -> ClarificationRequest:
    """Parse the worker-authored file; every section must carry substance.

    Requiring a recommendation is deliberate: it forces the worker to finish
    its own analysis before asking, so the channel cannot degrade into cheap
    "I'm confused" pings.
    """

    sections = _sections(text)
    required = ("blocker", "insufficiency", "options", "recommendation", "impact")
    missing = [name for name in required if not sections.get(name)]
    if missing:
        raise InvalidClarification(
            "clarification file is missing required sections: " + ", ".join(missing)
        )
    return ClarificationRequest(
        blocker=sections["blocker"][:_SECTION_LIMIT],
        insufficiency=sections["insufficiency"][:_SECTION_LIMIT],
        options=sections["options"][:_SECTION_LIMIT],
        recommendation=sections["recommendation"][:_SECTION_LIMIT],
        impact=sections["impact"][:_SECTION_LIMIT],
    )


def parse_clarification_file(path: Path) -> ClarificationRequest:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidClarification("clarification file cannot be read") from exc
    return parse_clarification(text)
