"""Steps shared by the real adapters: turn a ProcessOutput into parsed JSON or a failure."""

from __future__ import annotations

import json
from typing import Literal

from .base import AgentResult, ErrorKind, failure, looks_rate_limited
from .process import ProcessOutput


def parse_process_json(
    agent: Literal["claude", "agy"], out: ProcessOutput, timeout_s: int
) -> tuple[dict | None, AgentResult | None]:
    """Return (parsed_json, None) on success or (None, failed_result)."""
    tail = out.stderr_tail
    if out.not_found:
        return None, failure(agent, ErrorKind.NOT_FOUND, out.error, tail)
    if out.error:
        return None, failure(agent, ErrorKind.EXIT_NONZERO, out.error, tail)
    if out.timed_out:
        return None, failure(
            agent, ErrorKind.TIMEOUT, f"timed out after {timeout_s}s", tail, duration_ms=out.elapsed_ms
        )
    if out.returncode != 0:
        kind = ErrorKind.RATE_LIMITED if looks_rate_limited(out.stderr, out.stdout) else ErrorKind.EXIT_NONZERO
        return None, failure(
            agent,
            kind,
            f"exit code {out.returncode}: {(out.stderr or out.stdout).strip()[:500]}",
            tail,
            duration_ms=out.elapsed_ms,
        )
    if not out.stdout.strip():
        return None, failure(agent, ErrorKind.EMPTY_OUTPUT, "empty stdout", tail, duration_ms=out.elapsed_ms)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as e:
        return None, failure(
            agent, ErrorKind.BAD_JSON, f"invalid JSON ({e}): {out.stdout[:200]!r}", tail, duration_ms=out.elapsed_ms
        )
    if not isinstance(data, dict):
        return None, failure(
            agent, ErrorKind.BAD_JSON, "JSON output is not an object", tail, duration_ms=out.elapsed_ms
        )
    return data, None


def agent_error(
    agent: Literal["claude", "agy"], message: str, data: dict, out: ProcessOutput, **kw
) -> AgentResult:
    """AGENT_ERROR, upgraded to RATE_LIMITED if the error text or stderr matches a known pattern."""
    kind = ErrorKind.RATE_LIMITED if looks_rate_limited(message, out.stderr) else ErrorKind.AGENT_ERROR
    return failure(agent, kind, message, out.stderr_tail, raw=data, duration_ms=out.elapsed_ms, **kw)
