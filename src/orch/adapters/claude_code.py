"""Claude Code adapter: `claude -p` with the prompt on stdin."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .base import AgentAdapter, AgentRequest, AgentResult, ErrorKind
from .common import agent_error, parse_process_json
from .process import ProcessOutput, run_process

# Removed so claude always uses the Pro login instead of billing an API key.
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


@dataclass
class CommandSpec:
    argv: list[str]
    env: dict[str, str]
    stdin_text: str | None


def _structured(data: dict) -> dict | None:
    s = data.get("structured_output")
    return s if isinstance(s, dict) else None


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude"

    def build_command(self, request: AgentRequest) -> CommandSpec:
        argv = ["claude", "-p", "--output-format", "json"]
        if request.model:
            argv += ["--model", request.model]
        if request.json_schema is not None:
            argv += ["--json-schema", json.dumps(request.json_schema)]
        if request.resume_id:  # untested against the real CLI
            argv += ["--resume", request.resume_id]
        argv += request.extra_args
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
        return CommandSpec(argv=argv, env=env, stdin_text=request.prompt)

    def parse_output(self, out: ProcessOutput, request: AgentRequest) -> AgentResult:
        data, failed = parse_process_json("claude", out, request.timeout_s)
        if failed:
            return failed
        assert data is not None
        text = str(data.get("result") or "").strip()
        if data.get("is_error") or data.get("subtype", "success") != "success":
            msg = text or f"is_error={data.get('is_error')} subtype={data.get('subtype')}"
            return agent_error("claude", msg, data, out, session_id=data.get("session_id"))
        usage = data.get("usage") or {}
        inp = int(usage.get("input_tokens") or 0)
        outp = int(usage.get("output_tokens") or 0)
        c_read = int(usage.get("cache_read_input_tokens") or 0)
        c_write = int(usage.get("cache_creation_input_tokens") or 0)
        models = data.get("modelUsage") or {}
        cost = data.get("total_cost_usd")
        return AgentResult(
            agent="claude",
            ok=True,
            error_kind=ErrorKind.OK,
            text=text,
            structured=_structured(data),
            session_id=data.get("session_id"),
            model=next(iter(models), None) or "",
            duration_ms=int(data.get("duration_ms") or out.elapsed_ms),
            input_tokens=inp,
            output_tokens=outp,
            cache_read_tokens=c_read,
            cache_write_tokens=c_write,
            total_tokens=inp + outp + c_read + c_write,  # claude has no total field
            cost_usd=float(cost) if cost is not None else None,
            raw=data,
            stderr_tail=out.stderr_tail,
        )

    def run(self, request: AgentRequest) -> AgentResult:
        spec = self.build_command(request)
        out = run_process(
            spec.argv, cwd=request.cwd, env=spec.env, stdin_text=spec.stdin_text, timeout_s=request.timeout_s
        )
        result = self.parse_output(out, request)
        if result.model == "":
            result.model = request.model
        return result
