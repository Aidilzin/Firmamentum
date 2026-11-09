"""Helpers that build plan dicts and fake agent results for tests."""

from __future__ import annotations

import copy

from orch.adapters.base import AgentResult, ErrorKind


def step(sid="s1", deps=None, flags=None, files=None):
    return {
        "id": sid,
        "title": f"Do {sid}",
        "description": f"Change things for {sid} because the request needs it.",
        "files": files if files is not None else [f"src/{sid}.py"],
        "depends_on": deps or [],
        "acceptance": f"tests for {sid} pass",
        "risk_flags": flags or [],
    }


def good_plan(**over):
    p = {
        "goal": "Add a version command",
        "assumptions": ["package version comes from importlib.metadata"],
        "needs_input": False,
        "question": None,
        "test_command": "uv run pytest -q",
        "steps": [step("s1"), step("s2", deps=["s1"], flags=["new_dependency"])],
    }
    p.update(copy.deepcopy(over))
    return p


def result(structured=None, text="", ok=True, session_id="sess-1", tokens=100, agent="claude"):
    return AgentResult(
        agent=agent,
        ok=ok,
        error_kind=ErrorKind.OK if ok else ErrorKind.AGENT_ERROR,
        text=text,
        structured=structured,
        session_id=session_id,
        total_tokens=tokens,
    )
