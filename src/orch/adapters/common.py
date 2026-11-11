"""Steps shared by the real adapters: turn a ProcessOutput into parsed JSON or a failure."""

from __future__ import annotations

import json
from typing import Literal

from .base import AgentResult, ErrorKind, failure, looks_rate_limited
from .process import ProcessOutput


def structured_rate_limited(agent: Literal["claude", "agy"], data: dict | None) -> bool:
    """Rate limit signalled by the CLI's own structured fields. Never looks at model text or tool output.
    claude: `api_error_status` 429, or `subtype` / `terminal_reason`. agy: `status`."""
    if not isinstance(data, dict):
        return False
    def field_limited(*values: object) -> bool:
        raw = [str(v) for v in values if v]
        return looks_rate_limited(*raw, *(s.replace("_", " ").replace("-", " ") for s in raw))  # `error_rate_limit`

    if agent == "claude":
        if str(data.get("api_error_status")) == "429":
            return True
        return field_limited(data.get("subtype"), data.get("terminal_reason"))
    return field_limited(data.get("status"))


def _json_or_none(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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
        # stderr is the CLI's own log; stdout only counts through its structured fields (it may carry tool output)
        limited = looks_rate_limited(out.stderr) or structured_rate_limited(agent, _json_or_none(out.stdout))
        kind = ErrorKind.RATE_LIMITED if limited else ErrorKind.EXIT_NONZERO
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
    """AGENT_ERROR, upgraded to RATE_LIMITED if stderr or the CLI's structured error fields say so (never `message`,
    which is the model's own text)."""
    limited = looks_rate_limited(out.stderr) or structured_rate_limited(agent, data)
    kind = ErrorKind.RATE_LIMITED if limited else ErrorKind.AGENT_ERROR
    return failure(agent, kind, message, out.stderr_tail, raw=data, duration_ms=out.elapsed_ms, **kw)
