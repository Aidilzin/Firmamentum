"""FakeAdapter for tests: returns canned results, never spawns a process."""

from __future__ import annotations

from typing import Literal

from .base import AgentAdapter, AgentRequest, AgentResult, ErrorKind


class FakeAdapter(AgentAdapter):
    name = "claude"  # reported agent name; override per instance with `agent_name`

    def __init__(
        self,
        results: list[AgentResult] | None = None,
        *,
        text: str = "PONG",
        agent_name: Literal["claude", "agy"] = "claude",
    ):
        self._results = list(results or [])
        self._text = text
        self._agent = agent_name
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        if self._results:
            return self._results.pop(0) if len(self._results) > 1 else self._results[0]
        return AgentResult(
            agent=self._agent,
            ok=True,
            error_kind=ErrorKind.OK,
            text=self._text,
            session_id="fake-session",
            model=request.model,
            duration_ms=10,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
        )
