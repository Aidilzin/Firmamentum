from typer.testing import CliRunner

from orch import versions
from orch.cli import app

runner = CliRunner()


def test_version_prints_all(monkeypatch):
    monkeypatch.setattr(versions, "orch_version", lambda: "9.9.9")
    monkeypatch.setattr(versions, "cli_version", lambda name: f"{name}-ver")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "orch 9.9.9" in result.output
    assert "claude: claude-ver" in result.output
    assert "agy: agy-ver" in result.output


def test_version_missing_cli_still_exits_zero(monkeypatch):
    monkeypatch.setattr(versions, "orch_version", lambda: "9.9.9")
    monkeypatch.setattr(
        versions, "cli_version", lambda name: "not found" if name == "agy" else "2.0.0"
    )
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "claude: 2.0.0" in result.output
    assert "agy: not found" in result.output
