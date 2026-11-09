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
- claude: `--tools Read,Grep,Glob --permission-mode plan --permission-prompts none`, role text via `--append-system-prompt`, schema via `--json-schema`.
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
