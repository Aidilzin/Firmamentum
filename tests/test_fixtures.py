"""Checks the saved CLI fixtures have the fields orch will rely on. No real CLI calls."""

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_claude_fixture_fields():
    d = load("claude_pong.json")
    assert d["type"] == "result"
    assert d["subtype"] == "success"
    assert d["is_error"] is False
    assert d["result"].strip() == "PONG"
    assert isinstance(d["session_id"], str) and d["session_id"]
    assert isinstance(d["duration_ms"], int)
    u = d["usage"]
    for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        assert isinstance(u[key], int)


def test_agy_fixture_fields():
    d = load("agy_pong.json")
    assert d["status"] == "SUCCESS"
    assert d["response"].strip() == "PONG"
    assert isinstance(d["conversation_id"], str) and d["conversation_id"]
    assert isinstance(d["duration_seconds"], float)
    u = d["usage"]
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens"):
        assert isinstance(u[key], int)
