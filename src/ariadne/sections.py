"""Tolerant Markdown heading matching for the file-based contracts.

Both owner-facing contracts - the TASK and the worker's clarification file - are
specified to the model in English prose but written by it in the owner's
language.  Real drafts therefore number, bold, and gloss their headings
(`## objective（目标）`, `## 2. forbidden zones`, `## **Definition of Done**`).
Matching the raw heading text rejected those files as "missing required
sections", which stalled the pipeline on a purely cosmetic difference.

A heading is reduced instead to a short list of *exact* candidate names.
Candidates stay exact on purpose: prefix or substring matching would let
`## 允许改动以外的禁区` claim the allowed-files section.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

_HEADING = re.compile(r"^\s*#{1,6}\s+")
_HASHES = re.compile(r"^\s*#+")
_ENUMERATION = re.compile(r"^(?:\d+|[一二三四五六七八九十]+|[A-Za-z])[.)、．]\s*")
_BRACKETED = re.compile(r"[（(【\[]([^）)】\]]*)[）)】\]]")
_SEPARATORS = re.compile(r"[/|·・—–,，、]")


def build_alias_lookup(aliases: Mapping[str, Iterable[str]]) -> dict[str, str]:
    """Flatten `{section: {alias, ...}}` into `{alias: section}`."""

    return {alias.casefold(): name for name, values in aliases.items() for alias in values}


def _clean(text: str) -> str:
    return text.strip().strip("*_`~ ").strip().rstrip(":：").strip()


def heading_candidates(line: str) -> list[str]:
    """Exact names one heading may stand for, most specific first."""

    text = _clean(_ENUMERATION.sub("", _clean(_HASHES.sub("", line))))
    outside = _BRACKETED.sub(" ", text)
    candidates: list[str] = []
    for part in (text, outside, *_BRACKETED.findall(text), *_SEPARATORS.split(outside)):
        cleaned = _clean(part).casefold()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates


def section_name(line: str, alias_lookup: Mapping[str, str]) -> str | None:
    for candidate in heading_candidates(line):
        name = alias_lookup.get(candidate)
        if name is not None:
            return name
    return None


def collect_sections(text: str, alias_lookup: Mapping[str, str]) -> dict[str, str]:
    """Body text per recognised section; unrecognised headings end the previous one."""

    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if _HEADING.match(line):
            current = section_name(line, alias_lookup)
            if current:
                found.setdefault(current, [])
            continue
        if current:
            found[current].append(line)
    return {name: "\n".join(body).strip() for name, body in found.items()}
