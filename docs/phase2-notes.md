# Phase 2 notes

## Step 0a: stdin newlines
`process.py` now uses binary pipes. stdin is encoded to UTF-8 bytes by us (no LF to CRLF translation), and stdout/stderr are decoded as UTF-8 with `errors="replace"`. Test `test_stdin_newlines_are_not_translated` fails on the old code and passes now.

## Step 0b: CLI flags (from `claude --help` and `agy --help`, no quota used)

| Need | claude (`claude -p`) | agy (`agy -p=<prompt>`) |
|---|---|---|
| JSON schema | `--json-schema '<inline json>'` (print mode only). Help example is inline JSON. | `--json-schema` takes "a JSON schema string **or path to a schema file**". |
| Read-only / plan mode | `--permission-mode plan`; or restrict the toolset with `--tools "Read,Grep,Glob"` (built-in set only; `""` disables all) | `--mode plan` (other modes: `accept-edits`, `auto`) |
| Allow / deny tools | `--allowedTools` / `--disallowedTools` (comma or space separated, patterns like `Bash(git *)`) | No tool allow/deny flags. Only `--mode`, `--sandbox`, `--dangerously-skip-permissions`. |
| No prompt to a human | `--permission-prompts none` (anything that would prompt is denied) | n/a |
| Resume | `--resume <session-id>` (`-r`), `--continue` (`-c`), `--fork-session`, `--no-session-persistence` to disable | `--conversation <id>`, `--continue` / `-c` |
| System prompt | `--append-system-prompt <text>`, `--system-prompt <text>` (no `-file` variant in this version's help; `--bare` help mentions `-file` forms) | **None.** `--agent <name>` selects an agent only. Put instructions in the prompt. |
| Model | `--model <alias or full name>` | `--model <name>` (`agy models` lists) |
| Misc useful | `--fallback-model`, `--max-budget-usd`, `--effort`, `--add-dir`, `--system-prompt-snapshot` | `--effort`, `--add-dir`, `--print-timeout`, `--settings` |

Decisions for the Planner:
- claude: `--tools Read,Grep,Glob --permission-prompts none`, role text via `--append-system-prompt`, schema via `--json-schema`. `--permission-mode plan` is deliberately NOT used: the toolset is already read-only, and plan mode brings its own ExitPlanMode flow that could fight the structured-output reply. Untested which is safer; revisit if a real plan call misbehaves.
- agy scout: `--mode plan`, instructions inside the prompt.
- `--system-prompt-snapshot` is on by default: on `--resume` the first system prompt is reused, so a retry via resume keeps the planner text.
- Caveat: on Windows a long `--append-system-prompt` is part of argv (32k limit). `planner.md` must stay well under that.

## Step 0c: json-schema live check (nested schema with `$defs`/`$ref`)
Schema: object with `answer: string` and `items: array of $ref Item{id, n}`. Prompt asked for answer PONG and two items.
- **Both CLIs accept the schema inline** (`--json-schema '<json>'`) on Windows, `$defs`/`$ref` included. No need to inline refs or use a temp file. (agy would also accept a file path.)
- **Both return the parsed object in a top-level `structured_output` key**; `result` (claude) / `response` (agy) holds the same JSON as text. Added `AgentResult.structured`.
- agy also echoes the schema back under `json_schema`.
- claude: 24,054 tokens (nearly all cache creation), 2 turns (`num_turns: 2`, `stop_reason: tool_use` on the first). agy: 13,464 tokens.
- Not proven: that the CLIs *enforce* the schema when the model would otherwise deviate. Both complied on a trivial prompt. The planner still validates with pydantic and retries.
- Fixtures: `fixtures/claude_schema.json` is the exact raw stdout. `fixtures/agy_schema.json` was re-serialized from the parsed JSON (indent=2) because the first run printed it instead of saving stdout; content is identical, bytes are not.
- Real calls this step: claude x2 (the first output was truncated by my own print, so I repeated it with your OK), agy x1.

## Design decisions
- **One validation path.** `Plan.model_validate` checks shape and the step graph (unique ids, known deps, no self-dependency, no cycles, question iff needs_input, 1..8 steps unless needs_input). `src/orch/schemas/plan.json` is generated from the model (`uv run python -m orch.plan`) and a test fails if they drift. `$defs`/`$ref` are kept as pydantic emits them (both CLIs accept them).
- `risk_flags` is an enum of the 8 approval rules; `config.approval_rules` is validated against the same list, so a typo in config.yaml fails loudly.
- **Planner flow** (`planner.py`): optional scout (agy `--mode plan`) -> planner call (claude, `--tools Read,Grep,Glob`, `--permission-prompts none`, `planner.md` via `--append-system-prompt`, schema inline) -> parse `structured` (fallback: JSON from text, fences tolerated) -> validate -> on failure ONE retry. Retry resumes the session with the validation errors; if the resumed call itself fails to run, it falls back to a fresh call containing the prompt, the bad output and the errors. So worst case is 3 calls, never more than 1 *validation* retry. Agent errors (rate limit etc.) are not retried.
- For an agy planner the role text goes in the prompt (agy has no system-prompt flag).
- Scout is read-only via `--mode plan` only; agy has no tool allow-list, so that mode is the only guard.
- Runs live in `<repo>/.orch/runs/<UTC timestamp>-<slug>/` (collision gets `-2`); `.orch/` is already gitignored here. `orch runs/show` accept `--repo` and unique run-id prefixes. Failed runs keep `planner_raw.json` but have no `plan.json`.
- Config: `ORCH_CONFIG` env var overrides the config path (used by tests); default is `config.yaml` next to `src/` (works for the editable install only). Relative repo paths resolve against the config file's directory. `--repo` defaults to the current directory, with the configured test command if that directory is a configured repo.
- `--model role=model` changes only the model, never the agent.
- Reported by `orch plan`: run id, plan in execution order, status, total tokens (all calls, scout included), number of calls.

## Real check
`uv run orch plan "Add an \`orch version\` command ..."` on this repo, planner = claude opus, no scout.
- 1 call, valid on the first try, 22.5 s, 11 turns (file reads), model `claude-opus-5-5`, **87,485 tokens** (8 input, 2,265 output, 57,220 cache read, 27,992 cache write), $0.28 list cost.
- Plan: 2 steps (versions helper module with injectable runner, then the typer command), each with tests, no risk flags, sensible assumptions (importlib.metadata, `--version` costs no quota, 15 s timeout, scrubbed env for claude). Not implemented, per instructions.
- The run id timestamp is UTC, so it can look a day behind local time.

## Untested
- Retry via real `--resume` (only the fake path is tested); the real retry on a bad plan never happened.
- Whether the CLIs enforce the schema on a model that wants to deviate.
- `--scout` with real agy; agy as planner.
- Whether `agy --mode plan` really blocks edits (taken from help text).
- Windows argv limit with a very long `--append-system-prompt` (planner.md is ~3 KB).
- Whether `agy --version` exists (the plan from the real check assumes it; the implementation is not built).
- Non-default config path and running `orch` from outside the repo checkout.
