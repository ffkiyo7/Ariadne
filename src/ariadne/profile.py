"""Versioned project profiles for Ariadne.

The runner itself is deliberately repository-agnostic.  A profile contains
the small set of versioned Git/workflow facts that differ between projects;
machine paths and credentials remain in the private environment file.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


class ProfileError(ValueError):
    """A project profile is missing a required or safe value."""


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_BRANCH_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}/$")


def _string(section: Mapping[str, Any], key: str, *, default: str | None = None) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"profile field must be a non-empty string: {key}")
    return value.strip()


def _relative_directory(section: Mapping[str, Any], key: str, *, default: str) -> Path:
    value = _string(section, key, default=default).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise ProfileError(f"profile directory must be a safe relative path: {key}")
    return Path(*path.parts)


def _relative_markdown_file(section: Mapping[str, Any], key: str) -> Path | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(f"profile field must be a non-empty string: {key}")
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise ProfileError(f"profile field must be a safe relative Markdown path: {key}")
    return Path(*path.parts)


def _sibling_names(section: Mapping[str, Any]) -> tuple[str, ...]:
    value = section.get("protected_sibling_checkouts", [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProfileError("protected_sibling_checkouts must be a string list")
    names = tuple(item.strip() for item in value if item.strip())
    if any("/" in name or "\\" in name or name in {".", ".."} for name in names):
        raise ProfileError("protected sibling checkout names must be plain directory names")
    if len(set(names)) != len(names):
        raise ProfileError("protected sibling checkout names must be unique")
    return names


@dataclass(frozen=True)
class ProjectProfile:
    """Portable Git and workflow policy for one target project."""

    id: str
    git_remote: str
    base_branch: str
    branch_prefix: str
    plan_directory: Path
    task_directory: Path
    protected_sibling_checkouts: tuple[str, ...] = ()
    preview_required: bool = True
    # Optional in-repo shared knowledge file.  When set, it is silently added
    # to every TASK allowlist so a worker can append durable project findings
    # in the same reviewed commit.  AGENTS.md stays owner-curated and is
    # deliberately NOT writable through this mechanism.
    knowledge_file: Path | None = None

    @classmethod
    def default(cls) -> "ProjectProfile":
        """Safe generic fallback for legacy environment files.

        New deployments should pin ``ARIADNE_PROFILE`` so all Git policy is
        auditable in the standalone repository.  The fallback exists only to
        make the LuxrayKit state/config migration non-breaking.
        """

        return cls(
            id="generic",
            git_remote="origin",
            base_branch="main",
            branch_prefix="pipeline/",
            plan_directory=Path("docs/plans"),
            task_directory=Path("docs/tasks"),
        )

    @property
    def base_ref(self) -> str:
        return f"{self.git_remote}/{self.base_branch}"

    def branch_for(self, session_id: str) -> str:
        if not re.fullmatch(r"S-[0-9]{4}", session_id):
            raise ProfileError("invalid Ariadne session id")
        return f"{self.branch_prefix}{session_id}"

    def owns_branch(self, branch: str) -> bool:
        suffix = branch.removeprefix(self.branch_prefix)
        return branch.startswith(self.branch_prefix) and bool(re.fullmatch(r"S-[0-9]{4}", suffix))

    def plan_root(self, worktree: Path) -> Path:
        return Path(worktree).resolve() / self.plan_directory

    def task_root(self, worktree: Path) -> Path:
        return Path(worktree).resolve() / self.task_directory

    def forbidden_roots(self, repo: Path) -> tuple[Path, ...]:
        parent = Path(repo).resolve().parent
        return tuple((parent / name).resolve() for name in self.protected_sibling_checkouts)


def load_profile(path: Path) -> ProjectProfile:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ProfileError("ARIADNE_PROFILE must be an absolute path")
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProfileError("project profile cannot be read") from exc
    if not isinstance(payload, dict):  # pragma: no cover - tomllib always returns dict
        raise ProfileError("profile root must be a table")
    project = payload.get("project", {})
    git = payload.get("git", {})
    workflow = payload.get("workflow", {})
    if not all(isinstance(section, dict) for section in (project, git, workflow)):
        raise ProfileError("profile sections must be tables")
    identifier = _string(project, "id")
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ProfileError("project id must be lowercase letters, digits, or hyphens")
    remote = _string(git, "remote", default="origin")
    branch = _string(git, "base_branch", default="main")
    prefix = _string(git, "branch_prefix", default="ariadne/")
    if not _REMOTE_RE.fullmatch(remote) or not _REMOTE_RE.fullmatch(branch):
        raise ProfileError("Git remote and base branch contain unsafe characters")
    if not _BRANCH_PREFIX_RE.fullmatch(prefix) or ".." in PurePosixPath(prefix).parts:
        raise ProfileError("Git branch prefix is invalid")
    preview_required = workflow.get("preview_required", True)
    if not isinstance(preview_required, bool):
        raise ProfileError("workflow.preview_required must be true or false")
    return ProjectProfile(
        id=identifier,
        git_remote=remote,
        base_branch=branch,
        branch_prefix=prefix,
        plan_directory=_relative_directory(workflow, "plan_directory", default="docs/plans"),
        task_directory=_relative_directory(workflow, "task_directory", default="docs/tasks"),
        protected_sibling_checkouts=_sibling_names(workflow),
        preview_required=preview_required,
        knowledge_file=_relative_markdown_file(workflow, "knowledge_file"),
    )
