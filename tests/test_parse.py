"""Parse saved fixtures and hand-made error JSON into AgentResult. No real CLI calls."""

import json
from pathlib import Path

import pytest

from orch.adapters import AgentRequest, AntigravityAdapter, ClaudeCodeAdapter, ErrorKind
from orch.adapters.base import looks_rate_limited
from orch.adapters.process import ProcessOutput

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
REQ = AgentRequest(prompt="x", cwd=Path("."), model="sonnet")


def out(stdout="", stderr="", rc=0, **kw):
    return ProcessOutput(stdout=stdout, stderr=stderr, returncode=rc, elapsed_ms=42, **kw)


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_claude_fixture_all_fields():
    r = ClaudeCodeAdapter().parse_output(out(fixture_text("claude_pong.json")), REQ)
    assert r.ok and r.error_kind is ErrorKind.OK and r.agent == "claude"
    assert r.text == "PONG"
    assert r.session_id == "5ed47f49-9336-482c-a969-d76080a55eed"
    assert r.model == "claude-sonnet-5-5"
    assert r.duration_ms == 3121
    assert (r.input_tokens, r.output_tokens) == (2, 5)
    assert (r.cache_read_tokens, r.cache_write_tokens) == (14190, 9460)
    assert r.total_tokens == 2 + 5 + 14190 + 9460
    assert r.cost_usd == pytest.approx(0.039313)
    assert r.raw["type"] == "result"


def test_agy_fixture_all_fields():
    r = AntigravityAdapter().parse_output(out(fixture_text("agy_pong.json")), REQ)
    assert r.ok and r.error_kind is ErrorKind.OK and r.agent == "agy"
    assert r.text == "PONG"
    assert r.session_id == "b2614ec7-4914-4d54-9997-8aa11951a577"
    assert r.model == "sonnet"  # agy JSON has no model; the request model is echoed
    assert r.duration_ms == round(6.1957456 * 1000)
    assert (r.input_tokens, r.output_tokens) == (13270, 6)
    assert (r.cache_read_tokens, r.cache_write_tokens) == (0, 0)
    assert r.total_tokens == 13276
    assert r.cost_usd is None


def test_claude_is_error():
    d = {"is_error": True, "subtype": "success", "result": "boom"}
    r = ClaudeCodeAdapter().parse_output(out(json.dumps(d)), REQ)
    assert not r.ok and r.error_kind is ErrorKind.AGENT_ERROR and r.text == "boom"


def test_claude_non_success_subtype():
    d = {"is_error": False, "subtype": "error_max_turns", "result": ""}
    r = ClaudeCodeAdapter().parse_output(out(json.dumps(d)), REQ)
    assert not r.ok and r.error_kind is ErrorKind.AGENT_ERROR


def test_agy_status_error():
    d = {"status": "ERROR", "response": "it broke"}
    r = AntigravityAdapter().parse_output(out(json.dumps(d)), REQ)
    assert not r.ok and r.error_kind is ErrorKind.AGENT_ERROR and r.text == "it broke"


def test_rate_limit_message_in_json_and_stderr():
    d = {"is_error": True, "subtype": "success", "result": "5-hour usage limit reached"}
    r = ClaudeCodeAdapter().parse_output(out(json.dumps(d)), REQ)
    assert r.error_kind is ErrorKind.RATE_LIMITED
    d = {"status": "ERROR", "response": "oops"}
    r = AntigravityAdapter().parse_output(out(json.dumps(d), stderr="RESOURCE_EXHAUSTED"), REQ)
    assert r.error_kind is ErrorKind.RATE_LIMITED


def test_rate_limit_on_nonzero_exit():
    r = AntigravityAdapter().parse_output(out(stderr="HTTP 429 too many", rc=1), REQ)
    assert r.error_kind is ErrorKind.RATE_LIMITED


def test_429_only_as_whole_word():
    assert looks_rate_limited("error 429")
    assert not looks_rate_limited("used 14290 tokens, id a4297b")
    assert not looks_rate_limited("session 5429")


@pytest.mark.parametrize("adapter", [ClaudeCodeAdapter(), AntigravityAdapter()])
def test_failure_kinds(adapter):
    assert adapter.parse_output(out("not json"), REQ).error_kind is ErrorKind.BAD_JSON
    assert adapter.parse_output(out("[1]"), REQ).error_kind is ErrorKind.BAD_JSON
    assert adapter.parse_output(out("   "), REQ).error_kind is ErrorKind.EMPTY_OUTPUT
    assert adapter.parse_output(out(stderr="bad flag", rc=2), REQ).error_kind is ErrorKind.EXIT_NONZERO
    assert adapter.parse_output(out(timed_out=True, rc=1), REQ).error_kind is ErrorKind.TIMEOUT
    r = adapter.parse_output(ProcessOutput(not_found=True, error="no exe"), REQ)
    assert r.error_kind is ErrorKind.NOT_FOUND


def test_claude_structured_output_fixture():
    r = ClaudeCodeAdapter().parse_output(out(fixture_text("claude_schema.json")), REQ)
    assert r.ok
    assert r.structured == {"answer": "PONG", "items": [{"id": "a", "n": 1}, {"id": "b", "n": 2}]}


def test_agy_structured_output_fixture():
    r = AntigravityAdapter().parse_output(out(fixture_text("agy_schema.json")), REQ)
    assert r.ok
    assert r.structured == {"answer": "PONG", "items": [{"id": "a", "n": 1}, {"id": "b", "n": 2}]}


def test_structured_is_none_without_schema():
    assert ClaudeCodeAdapter().parse_output(out(fixture_text("claude_pong.json")), REQ).structured is None
    assert AntigravityAdapter().parse_output(out(fixture_text("agy_pong.json")), REQ).structured is None
