"""Command building: how each CLI receives the prompt, flags and env."""

import json
from pathlib import Path

from orch.adapters import AgentRequest, AntigravityAdapter, ClaudeCodeAdapter
from orch.adapters.antigravity import LONG_PROMPT_CHARS

SCRUBBED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def test_claude_prompt_on_stdin_and_flags(monkeypatch):
    for k in SCRUBBED:
        monkeypatch.setenv(k, "secret")
    monkeypatch.setenv("KEEP_ME", "1")
    schema = {"type": "object"}
    req = AgentRequest(
        prompt="hello", cwd=Path("."), model="sonnet", json_schema=schema,
        resume_id="abc", extra_args=["--permission-mode", "plan"],
    )
    spec = ClaudeCodeAdapter().build_command(req)
    assert spec.stdin_text == "hello"
    assert "hello" not in spec.argv
    assert spec.argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert spec.argv[spec.argv.index("--model") + 1] == "sonnet"
    assert json.loads(spec.argv[spec.argv.index("--json-schema") + 1]) == schema
    assert spec.argv[spec.argv.index("--resume") + 1] == "abc"
    assert spec.argv[-2:] == ["--permission-mode", "plan"]
    for k in SCRUBBED:
        assert k not in spec.env
    assert "KEEP_ME" not in spec.env and "PATH" in spec.env  # allow-list: unknown variables are not passed on


def test_claude_minimal_command_has_no_optional_flags():
    spec = ClaudeCodeAdapter().build_command(AgentRequest(prompt="p", cwd=Path(".")))
    assert spec.argv == ["claude", "-p", "--output-format", "json"]


def test_agy_prompt_is_last_argv_as_p_equals(tmp_path):
    req = AgentRequest(
        prompt='line1\n"PONG"', cwd=tmp_path, model="m", resume_id="cid", extra_args=["--mode", "plan"]
    )
    spec = AntigravityAdapter().build_command(req)
    assert spec.argv[-1] == '-p=line1\n"PONG"'
    assert "-p" not in spec.argv
    assert spec.argv[:3] == ["agy", "--output-format", "json"]
    assert spec.argv[spec.argv.index("--model") + 1] == "m"
    assert spec.argv[spec.argv.index("--conversation") + 1] == "cid"
    assert spec.prompt_file is None


def test_agy_long_prompt_uses_file(tmp_path):
    long_prompt = "x" * (LONG_PROMPT_CHARS + 1)
    spec = AntigravityAdapter().build_command(AgentRequest(prompt=long_prompt, cwd=tmp_path))
    f = spec.prompt_file
    assert f is not None and f.parent == tmp_path / ".orch" / "prompts"
    assert f.read_text(encoding="utf-8") == long_prompt
    assert spec.argv[-1] == f"-p=Read the file .orch/prompts/{f.name} and follow its instructions exactly."
    assert long_prompt not in " ".join(spec.argv)


def test_agy_prompt_at_limit_stays_inline(tmp_path):
    spec = AntigravityAdapter().build_command(AgentRequest(prompt="x" * LONG_PROMPT_CHARS, cwd=tmp_path))
    assert spec.prompt_file is None


def test_agy_long_prompt_file_deleted_after_run(tmp_path, monkeypatch):
    from orch.adapters import antigravity
    from orch.adapters.process import ProcessOutput

    seen = {}

    def fake_run(argv, **kw):
        seen["files"] = list((tmp_path / ".orch" / "prompts").glob("*.md"))
        return ProcessOutput(stdout='{"status":"SUCCESS","response":"ok","usage":{}}', returncode=0)

    monkeypatch.setattr(antigravity, "run_process", fake_run)
    r = AntigravityAdapter().run(AgentRequest(prompt="y" * 9000, cwd=tmp_path))
    assert r.ok
    assert len(seen["files"]) == 1  # existed during the call
    assert list((tmp_path / ".orch" / "prompts").glob("*.md")) == []  # removed afterwards
