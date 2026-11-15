"""One Session holds everything that used to be process-wide state (audit D7).

Loaded config with reload(), model overrides, theme and ascii, the default repo and the usage-DB path. It is passed down to
services and views; nothing here is a module global.

Thread rule: a Session is owned by the main thread and every mutation (`reload`, `set_*`) happens there. A worker thread never
receives the Session. The main thread takes `snapshot()` once per executor and hands the worker only that RunContext. The
worker never reads the theme either: it emits typed events and the main thread renders them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import budget
from .config import Config, ConfigError, ResolvedRepo, apply_overrides, default_config_path, load_config, resolve_repo
from .theme import Theme, resolve_theme


@dataclass(frozen=True)
class RunContext:
    """All a worker thread may hold for one run.

    `config` is a deep copy with the overrides applied: it is isolated from the Session (a reload or a later change to the
    session never reaches it), but a pydantic model is not immutable, so one snapshot belongs to one executor.
    Each start, resume, approve or answer takes a fresh snapshot, so a reload applies at the next stop, never inside a run."""

    config: Config
    repo: ResolvedRepo
    db_path: Path


class Session:
    def __init__(
        self,
        *,
        config_path: Path | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        theme: str | None = None,
        ascii_: bool = False,
        db_path: Path | None = None,
        stream=None,
        load: bool = True,
    ) -> None:
        self.config_path = Path(config_path) if config_path else default_config_path()
        self.cwd = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
        self.env: Mapping[str, str] = os.environ if env is None else env
        self.db_path = Path(db_path) if db_path else budget.default_db_path()
        self._stream = stream
        self._overrides: tuple[str, ...] = ()
        self._theme_name = theme  # the explicit choice (--theme); None follows ORCH_THEME and config ui.theme
        self._ascii = ascii_
        self._repo_arg: str | None = None
        self._base: Config | None = None
        self._applied: Config | None = None  # _base with the overrides; rebuilt when either changes
        self._theme: Theme | None = None
        self.config_error: str | None = None
        if load:
            self.reload()

    # ---- config ----
    def reload(self) -> None:
        """Re-read config.yaml into a NEW Config object. A failed reload keeps the old config and sets `config_error`."""
        try:
            new = load_config(self.config_path)
        except ConfigError as e:
            self.config_error = str(e)
            return
        self._base, self.config_error = new, None  # the old object is replaced, never modified
        self._applied = None
        if self._theme_name is None:
            self._theme = None  # the theme follows config ui.theme unless one was chosen explicitly

    @property
    def config_or_none(self) -> Config | None:
        """The loaded config, or None when it could not be loaded. Never raises."""
        return self._base

    @property
    def config(self) -> Config:
        """The config with the model overrides applied. A config error is raised before a bad override, as one-shot commands did."""
        if self._base is None:
            raise ConfigError(self.config_error or f"cannot read config {self.config_path}")
        if self._applied is None:
            self._applied = apply_overrides(self._base, list(self._overrides))
        return self._applied

    @property
    def overrides(self) -> tuple[str, ...]:
        return self._overrides

    def set_overrides(self, overrides: list[str] | None) -> None:
        """Store `role=model` strings. They are validated when `config` is read, not here."""
        self._overrides = tuple(overrides or ())
        self._applied = None

    # ---- theme ----
    @property
    def theme(self) -> Theme:
        """--theme > ORCH_THEME > config ui.theme > dark. Raises ValueError for an unknown name, as `resolve_theme` does."""
        if self._theme is None:
            cfg = self._base
            self._theme = resolve_theme(self._theme_name, self._ascii, cfg.ui.theme if cfg else None, self.env, self._stream)
        return self._theme

    def set_theme(self, name: str | None, ascii_: bool | None = None) -> None:
        self._theme_name = name
        if ascii_ is not None:
            self._ascii = ascii_
        self._theme = None

    # ---- repo ----
    @property
    def repo_arg(self) -> str | None:
        return self._repo_arg

    def set_repo(self, arg: str | None) -> None:
        self._repo_arg = arg

    def repo(self, arg: str | None = None) -> ResolvedRepo:
        """Resolved on every call, so a changed test_command applies to the next run. None falls back to the session repo, then cwd."""
        return resolve_repo(self.config, arg or self._repo_arg, cwd=self.cwd)

    # ---- worker hand-off ----
    def snapshot(self, repo_arg: str | None = None) -> RunContext:
        return RunContext(config=self.config.model_copy(deep=True), repo=self.repo(repo_arg), db_path=self.db_path)
