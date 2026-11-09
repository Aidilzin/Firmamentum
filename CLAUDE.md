# orch

Personal Python CLI that orchestrates two official coding CLIs as worker agents:
- Claude Code, run as `claude -p` (Claude Pro login)
- Antigravity CLI, run as `agy -p` (Google AI Pro login)

## Hard rules
- Never read, copy or use any OAuth token or credential file. Only spawn the official `claude` and `agy` executables.
- Before spawning `claude`, remove `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` from the child process env, so it always uses the Pro login.
- Keep real CLI calls to a minimum. Each call costs ~13k-20k tokens of plan quota for system prompts alone. Use tiny prompts like "Reply PONG".
- Use the cheapest models for experiments: `--model sonnet` for claude, the default model for agy.
- Ask before adding any dependency beyond the standard library and pytest.
- Tests never call the real CLIs.

## Stack
- Python 3.12, uv, pytest
- Run tests: `uv run pytest -q`
- Small commits.
