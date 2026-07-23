"""Environment configuration and validation.

Secrets are intentionally represented by a private dataclass field and never
appear in reprs or validation errors.  The state layer can therefore be used
without loading this module's secret values at all.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .models import Provider
from .profile import ProfileError, ProjectProfile, load_profile
from .reasoning import (
    CLAUDE_REASONING_EFFORTS,
    CODEX_REASONING_EFFORTS,
    DEFAULT_REASONING_EFFORT,
)


class ConfigError(ValueError):
    """Configuration is missing or violates a safety invariant."""


_SNOWFLAKE_RE = re.compile(r"^[0-9]{5,30}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"missing required configuration field: {key}")
    return value


def _path(env: Mapping[str, str], key: str, *, required: bool = True) -> Path | None:
    value = env.get(key, "").strip()
    if not value:
        if required:
            raise ConfigError(f"missing required configuration field: {key}")
        return None
    result = Path(value).expanduser()
    if not result.is_absolute():
        raise ConfigError(f"configuration field must be an absolute path: {key}")
    return result


def _path_alias(env: Mapping[str, str], primary: str, legacy: str) -> Path:
    """Read a renamed Ariadne field while keeping r9 env files usable."""

    if env.get(primary, "").strip():
        return _path(env, primary) or Path("/nonexistent")
    if env.get(legacy, "").strip():
        return _path(env, legacy) or Path("/nonexistent")
    raise ConfigError(f"missing required configuration field: {primary}")


def _snowflake(env: Mapping[str, str], key: str) -> str:
    value = _required(env, key)
    if not _SNOWFLAKE_RE.fullmatch(value):
        raise ConfigError(f"configuration field must be a Discord snowflake: {key}")
    return value


def _models(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    value = env.get(key, "").strip()
    if not value:
        return ()
    result = tuple(part.strip() for part in value.split(",") if part.strip())
    if not result or any(not _MODEL_RE.fullmatch(item) for item in result):
        raise ConfigError(f"configuration field contains an invalid model allowlist: {key}")
    if len(set(result)) != len(result):
        raise ConfigError(f"configuration field contains duplicate models: {key}")
    return result


def _efforts(env: Mapping[str, str], key: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    value = env.get(key, "").strip()
    if not value:
        return fallback
    result = tuple(part.strip().lower() for part in value.split(",") if part.strip())
    if not result or any(not _EFFORT_RE.fullmatch(item) for item in result):
        raise ConfigError(f"configuration field contains an invalid reasoning effort allowlist: {key}")
    if len(set(result)) != len(result):
        raise ConfigError(f"configuration field contains duplicate reasoning efforts: {key}")
    return result


def _boolean(env: Mapping[str, str], key: str, *, default: bool = False) -> bool:
    value = env.get(key, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"configuration field must be a boolean: {key}")


@dataclass(frozen=True, repr=False)
class Config:
    discord_token: str | None
    allowed_guild_id: str | None
    parent_channel_id: str | None
    owner_user_id: str | None
    harness_repo: Path
    worktree_root: Path
    state_dir: Path
    # Kept private like the Discord token.  It is deliberately optional so
    # interactive developer installations that rely on `gh auth login` remain
    # compatible; non-interactive services should configure it explicitly.
    github_token: str | None = None
    profile: ProjectProfile = field(default_factory=ProjectProfile.default)
    profile_path: Path | None = None
    max_concurrent_runs: int = 1
    codex_bin: Path | None = None
    codex_workspace_network_access: bool = False
    claude_bin: Path | None = None
    hermes_bin: Path | None = None
    codex_default_model: str | None = None
    codex_allowed_models: tuple[str, ...] = field(default_factory=tuple)
    claude_default_model: str | None = None
    claude_allowed_models: tuple[str, ...] = field(default_factory=tuple)
    codex_default_effort: str = DEFAULT_REASONING_EFFORT
    codex_allowed_efforts: tuple[str, ...] = CODEX_REASONING_EFFORTS
    claude_default_effort: str = DEFAULT_REASONING_EFFORT
    claude_allowed_efforts: tuple[str, ...] = CLAUDE_REASONING_EFFORTS

    def __repr__(self) -> str:
        return (
            "Config("
            f"allowed_guild_id={self.allowed_guild_id!r}, "
            f"parent_channel_id={self.parent_channel_id!r}, "
            f"owner_user_id={self.owner_user_id!r}, "
            f"harness_repo={str(self.harness_repo)!r}, "
            f"worktree_root={str(self.worktree_root)!r}, "
            f"state_dir={str(self.state_dir)!r}, "
            f"profile={self.profile.id!r}, "
            f"profile_path={str(self.profile_path) if self.profile_path else None!r}, "
            f"max_concurrent_runs={self.max_concurrent_runs!r}, "
            f"codex_bin={str(self.codex_bin) if self.codex_bin else None!r}, "
            f"codex_workspace_network_access={self.codex_workspace_network_access!r}, "
            f"claude_bin={str(self.claude_bin) if self.claude_bin else None!r}, "
            f"hermes_bin={str(self.hermes_bin) if self.hermes_bin else None!r}, "
            f"codex_default_model={self.codex_default_model!r}, "
            f"codex_allowed_models={self.codex_allowed_models!r}, "
            f"claude_default_model={self.claude_default_model!r}, "
            f"claude_allowed_models={self.claude_allowed_models!r}, "
            f"codex_default_effort={self.codex_default_effort!r}, "
            f"codex_allowed_efforts={self.codex_allowed_efforts!r}, "
            f"claude_default_effort={self.claude_default_effort!r}, "
            f"claude_allowed_efforts={self.claude_allowed_efforts!r})"
        )

    @property
    def project_repo(self) -> Path:
        """Clearer standalone name for the legacy HARNESS_REPO field."""

        return self.harness_repo

    @property
    def state_layout(self):
        from .filesystem import StateLayout

        return StateLayout.from_state_dir(self.state_dir)

    def allowed_models(self, provider: str) -> tuple[str, ...]:
        if provider == "codex":
            return self.codex_allowed_models
        if provider == "claude":
            return self.claude_allowed_models
        raise ConfigError("unknown provider")

    def default_model(self, provider: str) -> str:
        if provider == "codex":
            value = self.codex_default_model
        elif provider == "claude":
            value = self.claude_default_model
        else:
            raise ConfigError("unknown provider")
        if not value:
            raise ConfigError(f"missing default model for provider: {provider}")
        if value not in self.allowed_models(provider):
            raise ConfigError(f"default model is not in allowlist: {provider}")
        return value

    def allowed_efforts(self, provider: str) -> tuple[str, ...]:
        if provider == Provider.CODEX.value:
            return self.codex_allowed_efforts
        if provider == Provider.CLAUDE.value:
            return self.claude_allowed_efforts
        raise ConfigError("unknown provider")

    def default_effort(self, provider: str) -> str:
        if provider == Provider.CODEX.value:
            value = self.codex_default_effort
        elif provider == Provider.CLAUDE.value:
            value = self.claude_default_effort
        else:
            raise ConfigError("unknown provider")
        if value not in self.allowed_efforts(provider):
            raise ConfigError(f"default reasoning effort is not in allowlist: {provider}")
        return value

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_discord: bool = True,
    ) -> "Config":
        env = os.environ if environ is None else environ
        token = env.get("DISCORD_TOKEN", "").strip() or None
        github_token = env.get("ARIADNE_GITHUB_TOKEN", "").strip() or None
        if require_discord and token is None:
            raise ConfigError("missing required configuration field: DISCORD_TOKEN")
        guild = _snowflake(env, "DISCORD_ALLOWED_GUILD_ID") if require_discord else (
            _snowflake(env, "DISCORD_ALLOWED_GUILD_ID") if env.get("DISCORD_ALLOWED_GUILD_ID") else None
        )
        parent = _snowflake(env, "DISCORD_PARENT_CHANNEL_ID") if require_discord else (
            _snowflake(env, "DISCORD_PARENT_CHANNEL_ID") if env.get("DISCORD_PARENT_CHANNEL_ID") else None
        )
        owner = _snowflake(env, "DISCORD_OWNER_USER_ID") if require_discord else (
            _snowflake(env, "DISCORD_OWNER_USER_ID") if env.get("DISCORD_OWNER_USER_ID") else None
        )
        try:
            max_runs = int(env.get("MAX_CONCURRENT_RUNS", "1"))
        except ValueError as exc:
            raise ConfigError("MAX_CONCURRENT_RUNS must be a positive integer") from exc
        if max_runs != 1:
            raise ConfigError("MAX_CONCURRENT_RUNS must be 1 for harness v1")

        codex_bin = _path(env, "CODEX_BIN", required=False)
        claude_bin = _path(env, "CLAUDE_BIN", required=False)
        hermes_bin = _path(env, "HERMES_BIN", required=False)
        profile_path = _path(env, "ARIADNE_PROFILE", required=False)
        try:
            profile = load_profile(profile_path) if profile_path else ProjectProfile.default()
        except ProfileError as exc:
            raise ConfigError(str(exc)) from exc
        codex_allowed = _models(env, "CODEX_ALLOWED_MODELS")
        claude_allowed = _models(env, "CLAUDE_ALLOWED_MODELS")
        codex_default = env.get("CODEX_DEFAULT_MODEL", "").strip() or None
        claude_default = env.get("CLAUDE_DEFAULT_MODEL", "").strip() or None
        codex_allowed_efforts = _efforts(
            env, "CODEX_ALLOWED_REASONING_EFFORTS", CODEX_REASONING_EFFORTS
        )
        claude_allowed_efforts = _efforts(
            env, "CLAUDE_ALLOWED_REASONING_EFFORTS", CLAUDE_REASONING_EFFORTS
        )
        codex_default_effort = (
            env.get("CODEX_DEFAULT_REASONING_EFFORT", DEFAULT_REASONING_EFFORT).strip().lower()
            or DEFAULT_REASONING_EFFORT
        )
        claude_default_effort = (
            env.get("CLAUDE_DEFAULT_REASONING_EFFORT", DEFAULT_REASONING_EFFORT).strip().lower()
            or DEFAULT_REASONING_EFFORT
        )
        for provider, default, allowlist in (
            ("CODEX", codex_default, codex_allowed),
            ("CLAUDE", claude_default, claude_allowed),
        ):
            if default is not None and not _MODEL_RE.fullmatch(default):
                raise ConfigError(f"configuration field contains an invalid model: {provider}_DEFAULT_MODEL")
            if default is not None and not allowlist:
                raise ConfigError(f"default model requires a non-empty allowlist: {provider}")
            if default is not None and allowlist and default not in allowlist:
                raise ConfigError(f"default model is not in allowlist: {provider}")
        for provider, default, allowlist in (
            ("CODEX", codex_default_effort, codex_allowed_efforts),
            ("CLAUDE", claude_default_effort, claude_allowed_efforts),
        ):
            if not _EFFORT_RE.fullmatch(default):
                raise ConfigError(f"configuration field contains an invalid reasoning effort: {provider}")
            if not allowlist or default not in allowlist:
                raise ConfigError(f"default reasoning effort is not in allowlist: {provider}")
        for provider, allowlist, supported in (
            ("CODEX", codex_allowed_efforts, CODEX_REASONING_EFFORTS),
            ("CLAUDE", claude_allowed_efforts, CLAUDE_REASONING_EFFORTS),
        ):
            unsupported = sorted(set(allowlist) - set(supported))
            if unsupported:
                raise ConfigError(
                    f"configuration field contains unsupported reasoning effort for {provider}: "
                    + ", ".join(unsupported)
                )

        return cls(
            discord_token=token,
            allowed_guild_id=guild,
            parent_channel_id=parent,
            owner_user_id=owner,
            harness_repo=_path_alias(env, "ARIADNE_PROJECT_REPO", "HARNESS_REPO"),
            worktree_root=_path_alias(env, "ARIADNE_WORKTREE_ROOT", "WORKTREE_ROOT"),
            state_dir=_path_alias(env, "ARIADNE_STATE_DIR", "HARNESS_STATE_DIR"),
            github_token=github_token,
            profile=profile,
            profile_path=profile_path,
            max_concurrent_runs=max_runs,
            codex_bin=codex_bin,
            codex_workspace_network_access=_boolean(env, "CODEX_WORKSPACE_NETWORK_ACCESS"),
            claude_bin=claude_bin,
            hermes_bin=hermes_bin,
            codex_default_model=codex_default,
            codex_allowed_models=codex_allowed,
            claude_default_model=claude_default,
            claude_allowed_models=claude_allowed,
            codex_default_effort=codex_default_effort,
            codex_allowed_efforts=codex_allowed_efforts,
            claude_default_effort=claude_default_effort,
            claude_allowed_efforts=claude_allowed_efforts,
        )
