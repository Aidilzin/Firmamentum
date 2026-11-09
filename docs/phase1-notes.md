# Phase 1 notes: adapter layer

## Design decisions
- `AgentAdapter.run(request) -> AgentResult` never raises. Every failure maps to an `ErrorKind`.
- Each real adapter has a pure `build_command` and a pure `parse_output`, so tests need no subprocess. `adapters/common.py` holds the shared exit/empty/JSON classification.
- `adapters/process.py` is the only place that spawns processes: `Popen`, no shell, UTF-8 with `errors="replace"` for stdin/stdout/stderr, `CREATE_NEW_PROCESS_GROUP` on Windows, `taskkill /T /F` on timeout (process group kill elsewhere). A test confirms a grandchild process dies too.
- claude gets the prompt on stdin and a scrubbed env (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` removed). agy gets `-p=<prompt>` as the last argv item. Prompts over 8000 chars go to `<cwd>/.orch/prompts/<uuid>.md` and are deleted after the call.
- `total_tokens`: claude = input + output + cache read + cache write (no total field); agy = `usage.total_tokens`. agy's JSON has no model, so `model` is the requested model (`None` = CLI default).
- Rate limits: `RATE_LIMIT_PATTERNS` in `adapters/base.py` (single list, `\b429\b` whole word). Checked on JSON error text and stderr, also for non-zero exits.
- Usage log: one global SQLite file (`%LOCALAPPDATA%\orch\usage.db`, or `~/.local/share/orch/usage.db`), because plan quota is shared across projects. `budget.run_agent` is the one path every call takes. Tests redirect the path (`tests/conftest.py`).

## Real check (Phase 1)
- `orch ask --agent claude --model sonnet --prompt "Reply PONG"` -> `PONG`, 7.9 s, 23,757 tokens (mostly cache).
- `orch ask --agent agy --prompt "Reply PONG"` -> `PONG`, 5.6 s, 13,268 tokens.
- A multi-line prompt with double quotes (`Reply with exactly this, on two lines:` / `"PONG"` / `done`) through `-p=<prompt>` on Windows came back intact: `"PONG"\ndone`. So newlines and quotes survive the argument.
- `orch usage` showed both agents, 0 failures.

## Untested
- `resume_id` (`claude --resume`, `agy --conversation`) is wired but never run against a real CLI.
- `json_schema` (`--json-schema`) flags are built and unit-tested, but not run for real. Check the exact accepted form (inline JSON vs file path) before Phase 2 relies on it.
- Real rate-limit messages are unknown. Add them to `RATE_LIMIT_PATTERNS` when seen.
- The long-prompt file path for agy was tested with mocks only; agy reading the file was not verified.
- Prompts with stdin newline translation on Windows (text mode may turn `\n` into `\r\n` for claude's stdin) were not checked against a real reply.
- Non-Windows process-group kill is untested.
