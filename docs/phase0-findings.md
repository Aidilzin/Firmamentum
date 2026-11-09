# Phase 0 findings: driving `claude -p` and `agy -p` headless on Windows

Both CLIs run headless via `subprocess.run` (no shell, stdout/stderr piped, 120 s timeout) and return parseable JSON. Test prompt: `Reply PONG`. Both replied `PONG`.

## Working command lines

| | claude | agy |
|---|---|---|
| Command | `claude -p --output-format json --model sonnet` | `agy --output-format json -p=<prompt>` |
| Prompt passed via | **stdin** (works) | **argument** (stdin not supported) |
| Env | Remove `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` from the child env | Inherited env unchanged |
| Model | `--model sonnet` | default |
| Time (PONG) | ~4.8 s wall | ~11.7 s wall |

`spike.py` implements both (`spike_claude`, `spike_agy`).

## JSON fields we will need

Key names are taken from `fixtures/claude_pong.json` and `fixtures/agy_pong.json`.

| Need | claude | agy |
|---|---|---|
| Reply text | `result` | `response` (trailing `\n`; strip it) |
| Success / error | `is_error` (bool), `subtype` (`"success"`), `terminal_reason`, `api_error_status` | `status` (`"SUCCESS"`; treat any other value as failure) |
| Session / conversation id | `session_id` | `conversation_id` |
| Input tokens | `usage.input_tokens` | `usage.input_tokens` |
| Output tokens | `usage.output_tokens` | `usage.output_tokens` |
| Cached tokens | `usage.cache_read_input_tokens` (+ `usage.cache_creation_input_tokens`) | `usage.cache_read_tokens` |
| Total tokens | not provided (sum the parts) | `usage.total_tokens` |
| Duration | `duration_ms` (also `duration_api_ms`) | `duration_seconds` (float, **seconds**, not ms) |
| Cost | `total_cost_usd` | not provided |
| Turns | `num_turns` | `num_turns` |

Unit difference: claude duration is in milliseconds, agy in seconds. Normalize in the adapter.

## Problems found and solutions

1. **agy `-p` swallows the next token as its prompt.** `agy -p --output-format json "Reply PONG"` fails with exit 2: `-p took "--output-format" as its prompt...`. Fix: put the prompt on the flag with `=`, i.e. `agy --output-format json -p=<prompt>`. This failed fast with no quota used.
2. **agy does not read the prompt from stdin.** With `agy --output-format json -p` and the prompt on stdin, it prints its usage text and exits 2. Falling back to an argument is required for agy only. Windows quoting is handled because we pass an argv list with no shell. Very long prompts could hit the ~32k command-line limit on Windows, so later phases need a plan (e.g. have the prompt reference a file in the workdir).
3. **The reported "agy prints nothing when stdout is not a terminal" bug did not reproduce.** With stdout piped, `--output-format json` returned the full JSON object. The `stream-json` workaround was therefore not needed and was not tested.
4. **claude token usage looks small but is not.** `usage.input_tokens` was 2, and the real cost sits in the cache fields (about 14k read and 9k created), so adapters must add the cache fields when accounting for quota. agy reports about 13k `input_tokens` with 0 cached.
5. Keep claude's scrubbed env: with `ANTHROPIC_*` removed it used the Pro login (`modelUsage` showed `provider: firstParty`).

## Failure handling implemented in spike.py

Exe not found (`shutil.which`), timeout, non-zero exit (stderr tail included), empty stdout, invalid JSON, and an error result inside valid JSON (`is_error` / non-`success` subtype for claude, non-`SUCCESS` status for agy).

## Quota used

One real call to claude and one successful real call to agy (about 13k input tokens each). The failed agy attempts (flag-parsing error, stdin usage error) never reached the model.

## Not tested (left for later phases)

- `stream-json` output and `--input-format stream-json`
- Tool use, permissions modes and working directory control
- Session resume (`--resume` for claude, `--conversation` / `--continue` for agy)
