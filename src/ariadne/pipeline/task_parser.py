"""Parse and enforce the Markdown TASK contract."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..sections import build_alias_lookup, collect_sections


class InvalidTask(ValueError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    path: Path
    objective: str
    allowed_files: tuple[str, ...]
    forbidden_zones: str
    interfaces: str
    definition_of_done: str
    verification_commands: tuple[str, ...]

    def allows_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or ".." in PurePosixPath(normalized).parts:
            return False
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in self.allowed_files)

    def validate_changed_paths(self, paths: list[str] | tuple[str, ...]) -> None:
        invalid = [path for path in paths if not self.allows_path(path)]
        if invalid:
            raise InvalidTask("changed files fall outside the TASK allowlist")

    def validate_hermes_contract(self) -> None:
        """Reject a TASK that conflicts with Ariadne's fixed Git lifecycle.

        Hermes is required to make exactly one *local* implementation commit
        before a clean reviewed branch can be pushed by the later owner-gated
        Draft-PR action.  The task author controls the product scope, not that
        lifecycle rule; leaving an explicit "no commits" instruction in the
        forbidden-zones section gives Hermes contradictory instructions.
        """

        for raw_line in self.forbidden_zones.splitlines():
            line = raw_line.strip().lstrip("-*• ").strip().strip("`*_ ").casefold()
            if re.fullmatch(r"(?:git\s+)?(?:local\s+)?commits?", line):
                raise InvalidTask(
                    "TASK forbidden zones may not prohibit Ariadne's required local commit"
                )
        normalized = " ".join(self.forbidden_zones.casefold().split())
        if re.search(r"\b(?:do not|don't|never|no)\b.{0,24}\b(?:local\s+)?commits?\b", normalized):
            raise InvalidTask(
                "TASK forbidden zones may not prohibit Ariadne's required local commit"
            )
        if re.search(r"(?:不要|禁止|不得|不可).{0,16}(?:本地)?提交", self.forbidden_zones):
            raise InvalidTask(
                "TASK forbidden zones may not prohibit Ariadne's required local commit"
            )

    def prompt_constraints(self, *, worktree: Path, branch: str) -> str:
        files = "\n".join(f"- `{path}`" for path in self.allowed_files)
        commands = "\n".join(f"- `{command}`" for command in self.verification_commands)
        return (
            f"Worktree: `{worktree}`\n"
            f"Branch: `{branch}`\n\n"
            f"Objective:\n{self.objective}\n\n"
            f"Allowed files only:\n{files}\n\n"
            f"Forbidden zones:\n{self.forbidden_zones}\n\n"
            f"Interfaces/constraints:\n{self.interfaces}\n\n"
            f"Definition of done:\n{self.definition_of_done}\n\n"
            f"Verification commands:\n{commands}\n\n"
            "Ariadne requires exactly one local commit containing only the allowed changes after "
            "verification. Do not push, merge, reset, clean, expose secrets, or expand this scope."
        )


_ALIASES = {
    "objective": {"目标", "目的", "objective", "objectives", "goal"},
    "allowed": {
        "允许改动",
        "允许修改文件",
        "允许改动文件",
        "允许的文件",
        "allowed files",
        "allowed paths",
        "allowed changes",
    },
    "forbidden": {"禁区", "禁止事项", "禁止改动", "forbidden", "forbidden zones", "不可做"},
    "interfaces": {
        "接口",
        "接口约定",
        "实施要求",
        "interfaces",
        "constraints",
        "implementation requirements",
    },
    "dod": {"dod", "definition of done", "完成定义", "完成条件", "验收标准"},
    "verification": {
        "验证",
        "验证命令",
        "验证步骤",
        "验收命令",
        "verification",
        "verification commands",
        "verification steps",
    },
}

_ALIAS_LOOKUP = build_alias_lookup(_ALIASES)


def _sections(text: str) -> dict[str, str]:
    return collect_sections(text, _ALIAS_LOOKUP)


_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def _looks_like_path(value: str) -> bool:
    """Whether one token is a path or glob rather than prose or an identifier."""

    if not value or any(character.isspace() for character in value):
        return False
    if any(character in value for character in "/\\*?"):
        return True
    return bool(re.fullmatch(r"[\w.\-+@]+\.[A-Za-z0-9_]+", value))


def _parse_allowed(body: str) -> tuple[str, ...]:
    values: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        stripped = line.strip()
        if not in_fence and not stripped.startswith("|"):
            # A bullet routinely annotates its path ("新增 `src/a.ts`——类型定义"),
            # and the annotation itself may contain slashed prose.  When the line
            # marks paths up as code spans, only those spans are the allowlist.
            spans = [span for span in _CODE_SPAN.findall(stripped) if _looks_like_path(span)]
            if spans:
                values.extend(spans)
                continue
        item = stripped.lstrip("-* ").strip().strip("`")
        if not item or item.startswith("#"):
            continue
        if item.startswith("|"):
            continue
        # Avoid interpreting prose as a filename while accepting safe root
        # files such as `README.md` and `package.json`.  TASKs may also use a
        # table, a fenced block, or one path per bullet.
        if "/" not in item and "\\" not in item and not in_fence:
            if "." not in item and not any(marker in item for marker in ("*", "?")):
                continue
        item = item.split("  #", 1)[0].strip().strip("`")
        if item:
            values.append(item)
    if not values:
        raise InvalidTask("TASK is missing an allowed-files list")
    normalized: list[str] = []
    for value in values:
        value = value.replace("\\", "/")
        if value.startswith("/") or ".." in PurePosixPath(value).parts:
            raise InvalidTask("TASK allowlist contains an unsafe path")
        normalized.append(value)
    return tuple(dict.fromkeys(normalized))


def _verification_commands(body: str) -> tuple[str, ...]:
    commands: list[str] = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if in_fence:
            commands.append(stripped)
        elif stripped.startswith(("- ", "* ")):
            value = stripped[2:].strip().strip("`")
            if value:
                commands.append(value)
    if not commands:
        raise InvalidTask("TASK is missing verification commands")
    return tuple(commands)


def parse_task(path: Path) -> TaskSpec:
    path = Path(path)
    if not path.is_absolute():
        raise InvalidTask("TASK path must be absolute")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidTask("TASK file cannot be read") from exc
    sections = _sections(text)
    required = ("objective", "allowed", "forbidden", "interfaces", "dod", "verification")
    missing = [name for name in required if not sections.get(name)]
    if missing:
        raise InvalidTask("TASK is missing required sections")
    return TaskSpec(
        path=path,
        objective=sections["objective"],
        allowed_files=_parse_allowed(sections["allowed"]),
        forbidden_zones=sections["forbidden"],
        interfaces=sections["interfaces"],
        definition_of_done=sections["dod"],
        verification_commands=_verification_commands(sections["verification"]),
    )
