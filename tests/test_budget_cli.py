"""Budget logging, FakeAdapter end to end, and the typer CLI."""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from orch import budget, cli
from orch.adapters import AgentRequest, ErrorKind, FakeAdapter
from orch.adapters.base import failure

runner = CliRunner()


def test_run_agent_logs_one_row_per_call(tmp_path):
    db = tmp_path / "t.db"
    req = AgentRequest(prompt="p", cwd=tmp_path, model="m")
    r1 = budget.run_agent(FakeAdapter(), req, db)
    budget.run_agent(FakeAdapter([failure("agy", ErrorKind.TIMEOUT, "slow")]), req, db)
    assert r1.ok and r1.text == "PONG"
    rows = sqlite3.connect(db).execute(
        "SELECT agent, model, ok, error_kind, total_tokens, cwd FROM calls ORDER BY id"
    ).fetchall()
    assert rows == [
        ("claude", "m", 1, "OK", 15, str(tmp_path)),
        ("agy", None, 0, "TIMEOUT", 0, str(tmp_path)),
    ]


def test_summary_per_agent(tmp_path):
    db = tmp_path / "t.db"
    req = AgentRequest(prompt="p", cwd=tmp_path)
    budget.run_agent(FakeAdapter(), req, db)
    budget.run_agent(FakeAdapter(), req, db)
    budget.run_agent(FakeAdapter([failure("claude", ErrorKind.AGENT_ERROR, "x")]), req, db)
    s = budget.summary(datetime.now(timezone.utc) - timedelta(days=1), db)
    assert [(x.agent, x.calls, x.tokens, x.failures) for x in s] == [("claude", 3, 30, 1)]
    assert budget.summary(datetime.now(timezone.utc) + timedelta(days=1), db) == []


def test_default_db_path_is_global_not_cwd():
    from conftest import REAL_DEFAULT_DB_PATH  # the autouse fixture patches the module attribute

    real = REAL_DEFAULT_DB_PATH()
    assert real.name == "usage.db" and real.parent.name == "orch"
    assert str(real).startswith(str(Path.home()))


def test_fake_adapter_non_ascii(tmp_path):
    r = budget.run_agent(FakeAdapter(text="café ✓ 日本語"), AgentRequest(prompt="é", cwd=tmp_path))
    assert r.text == "café ✓ 日本語"


def _patch(monkeypatch, fake):
    monkeypatch.setattr(cli, "get_adapter", lambda name: fake)


def test_cli_ask_prompt(monkeypatch, tmp_path):
    fake = FakeAdapter(text="café ✓")
    _patch(monkeypatch, fake)
    args = ["ask", "--agent", "claude", "--prompt", "hi", "--model", "m", "--cwd", str(tmp_path)]
    res = runner.invoke(cli.app, args)
    assert res.exit_code == 0, res.output
    assert "café ✓" in res.output and "15 tokens" in res.output
    assert fake.requests[0].prompt == "hi" and fake.requests[0].model == "m"


def test_cli_ask_file_and_json(monkeypatch, tmp_path):
    f = tmp_path / "p.md"
    f.write_text("from file é", encoding="utf-8")
    fake = FakeAdapter()
    _patch(monkeypatch, fake)
    res = runner.invoke(cli.app, ["ask", "--agent", "agy", "--file", str(f), "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["text"] == "PONG"
    assert fake.requests[0].prompt == "from file é"


def test_cli_ask_requires_exactly_one_prompt_source(monkeypatch):
    _patch(monkeypatch, FakeAdapter())
    assert runner.invoke(cli.app, ["ask", "--agent", "claude"]).exit_code == 2
    both = ["ask", "--agent", "claude", "--prompt", "a", "--file", "b"]
    assert runner.invoke(cli.app, both).exit_code == 2


def test_cli_ask_failure_exit_code(monkeypatch):
    _patch(monkeypatch, FakeAdapter([failure("claude", ErrorKind.RATE_LIMITED, "limit")]))
    res = runner.invoke(cli.app, ["ask", "--agent", "claude", "--prompt", "hi"])
    assert res.exit_code == 1 and "RATE_LIMITED" in res.output


def test_cli_usage(monkeypatch):
    _patch(monkeypatch, FakeAdapter())
    runner.invoke(cli.app, ["ask", "--agent", "claude", "--prompt", "hi"])
    res = runner.invoke(cli.app, ["usage", "--days", "1"])
    assert res.exit_code == 0 and "claude" in res.output


def test_cli_usage_json(monkeypatch):
    _patch(monkeypatch, FakeAdapter())
    runner.invoke(cli.app, ["ask", "--agent", "claude", "--prompt", "hi"])
    res = runner.invoke(cli.app, ["usage", "--days", "1", "--json"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["days"] == 1
    assert data["agents"] == [{"agent": "claude", "calls": 1, "tokens": 15, "failures": 0}]


def test_cli_usage_json_empty():
    res = runner.invoke(cli.app, ["usage", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["agents"] == []
