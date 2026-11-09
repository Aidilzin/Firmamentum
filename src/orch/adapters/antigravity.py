"""Antigravity adapter: `agy --output-format json ... -p=<prompt>` (agy cannot read stdin)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from .base import AgentAdapter, AgentRequest, AgentResult, ErrorKind
from .common import agent_error, parse_process_json
from .process import ProcessOutput, child_env, run_process

LONG_PROMPT_CHARS = 8000


@dataclass
class CommandSpec:
    argv: list[str]
    prompt_file: Path | None = None  # deleted after the call
    env: dict[str, str] | None = None  # parent env minus VIRTUAL_ENV*


class AntigravityAdapter(AgentAdapter):
    name = "agy"

    def build_command(self, request: AgentRequest) -> CommandSpec:
        prompt = request.prompt
        prompt_file: Path | None = None
        if len(prompt) > LONG_PROMPT_CHARS:
            prompt_dir = request.cwd / ".orch" / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            prompt_id = uuid.uuid4().hex
            prompt_file = prompt_dir / f"{prompt_id}.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            prompt = f"Read the file .orch/prompts/{prompt_id}.md and follow its instructions exactly."
        argv = ["agy", "--output-format", "json"]
        if request.model:
            argv += ["--model", request.model]
        if request.json_schema is not None:
            argv += ["--json-schema", json.dumps(request.json_schema)]
        if request.resume_id:  # untested against the real CLI
            argv += ["--conversation", request.resume_id]
        argv += request.extra_args
        # -p must carry its prompt as "-p=<prompt>" and come last, or agy swallows the next token.
        argv.append(f"-p={prompt}")
        return CommandSpec(argv=argv, prompt_file=prompt_file, env=child_env())

    def parse_output(self, out: ProcessOutput, request: AgentRequest) -> AgentResult:
        data, failed = parse_process_json("agy", out, request.timeout_s)
        if failed:
            return failed
        assert data is not None
        text = str(data.get("response") or "").strip()
        status = data.get("status")
        if status != "SUCCESS":
            msg = text or f"status={status!r}"
            return agent_error("agy", msg, data, out, session_id=data.get("conversation_id"))
        usage = data.get("usage") or {}
        duration_s = data.get("duration_seconds")
        return AgentResult(
            agent="agy",
            ok=True,
            error_kind=ErrorKind.OK,
            text=text,
            structured=data["structured_output"] if isinstance(data.get("structured_output"), dict) else None,
            session_id=data.get("conversation_id"),
            model=request.model,  # agy's JSON does not report the model
            duration_ms=int(round(float(duration_s) * 1000)) if duration_s is not None else out.elapsed_ms,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            cache_write_tokens=0,
            total_tokens=int(usage.get("total_tokens") or 0),
            cost_usd=None,
            raw=data,
            stderr_tail=out.stderr_tail,
        )

    def run(self, request: AgentRequest) -> AgentResult:
        spec = self.build_command(request)
        try:
            out = run_process(spec.argv, cwd=request.cwd, env=spec.env, timeout_s=request.timeout_s)
            return self.parse_output(out, request)
        finally:
            if spec.prompt_file is not None:
                spec.prompt_file.unlink(missing_ok=True)
