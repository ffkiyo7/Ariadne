"""Hermes NEEDS_CLARIFICATION protocol.

A worker turn has exactly two terminal outcomes: one implementation commit,
or one `ARIADNE-CLARIFICATION.md` file at the worktree root with no other
change.  This module owns the file contract; detection and enforcement live
in the runner's completion gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .sections import build_alias_lookup, collect_sections


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


_ALIAS_LOOKUP = build_alias_lookup(_ALIASES)


def _sections(text: str) -> dict[str, str]:
    return collect_sections(text, _ALIAS_LOOKUP)


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
