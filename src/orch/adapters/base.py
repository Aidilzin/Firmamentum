"""Adapter interface and shared models. Nothing above the adapters imports a specific CLI."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class ErrorKind(str, Enum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    EXIT_NONZERO = "EXIT_NONZERO"
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    BAD_JSON = "BAD_JSON"
    AGENT_ERROR = "AGENT_ERROR"
    RATE_LIMITED = "RATE_LIMITED"


# No real limit message has been seen yet. Add the real ones here (case-insensitive regex).
RATE_LIMIT_PATTERNS: list[str] = [
    r"usage limit",
    r"rate limit",
    r"\b429\b",
    r"resource_exhausted",
    r"quota",
    r"session limit",
]
_RATE_LIMIT_RE = re.compile("|".join(f"(?:{p})" for p in RATE_LIMIT_PATTERNS), re.IGNORECASE)


def looks_rate_limited(*texts: str | None) -> bool:
    return any(t and _RATE_LIMIT_RE.search(t) for t in texts)


class AgentRequest(BaseModel):
    prompt: str
    cwd: Path
    model: str | None = None  # passed to --model; None = CLI default
    timeout_s: int = 600
    json_schema: dict | None = None  # passed to --json-schema when set
    resume_id: str | None = None  # session/conversation to continue (wired up, UNTESTED)
    extra_args: list[str] = Field(default_factory=list)  # e.g. permission flags, set by later phases


class AgentResult(BaseModel):
    agent: Literal["claude", "agy"]
    ok: bool
    error_kind: ErrorKind
    text: str = ""
    structured: dict | None = None  # parsed --json-schema result, when the CLI returns one
    session_id: str | None = None
    model: str | None = None
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    raw: dict | None = None
    stderr_tail: str = ""  # last 2000 chars of stderr


class AgentAdapter(ABC):
    """One worker CLI. `run` never raises: every failure becomes an AgentResult."""

    name: ClassVar[Literal["claude", "agy"]]

    @abstractmethod
    def run(self, request: AgentRequest) -> AgentResult: ...


def failure(
    agent: Literal["claude", "agy"], kind: ErrorKind, text: str, stderr_tail: str = "", **kw
) -> AgentResult:
    """Build a failed AgentResult; `text` holds a human-readable reason."""
    return AgentResult(agent=agent, ok=False, error_kind=kind, text=text, stderr_tail=stderr_tail, **kw)
