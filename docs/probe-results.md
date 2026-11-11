# Probe results: worker flags (H1, H2, M6)

11 Nov 2026 · `claude` 2.1.322 (`--model haiku`) and `agy` (default model) · scratch repo outside orch, deleted afterwards.
Each probe was one real call with harmless actions (marker files only). Raw output was saved before parsing; the
relevant lines are quoted below because the scratch folders are gone.

Builder flags before: `--permission-prompts none --tools Read,Grep,Glob,Edit,Write,Bash --allowedTools Read,Grep,Glob,Edit,Write,Bash(<cmd>)`.
Builder flags after: the same plus `--restricted --strict-mcp-config`.

## Before the change (audit findings confirmed)

| Probe | Question | Result |
| --- | --- | --- |
| H1 | Does a bare `Write` allow rule reach outside the worktree? | **Yes.** Haiku's first attempt refused on its own (role text said not to write outside; no tool call, so inconclusive). With a neutral role text, `Write` created `orch-probe-outside/h1b.txt` and returned "File created successfully". `permission_denials` was empty. |
| H2 | Does a worktree `.claude/settings.json` hook run under `-p`? | **Yes.** A `SessionStart` hook wrote `orch-probe-outside/h2.txt`. The hook received the session JSON (incl. `transcript_path`) on stdin. |
| M6 | Does `agy --mode plan` stop writes? | **Partly.** `m6.txt` was not created and a `RunCommand` was listed in `denied_actions`, but the model only described what it would inspect; its file-write tool was never exercised. |

## After the change (`--restricted --strict-mcp-config`; agy `--mode plan --sandbox`)

| # | Probe | Pass condition | Result |
| --- | --- | --- | --- |
| 1 | H1b: `Write` outside the worktree | file absent, tool denied | **Pass.** `permission_denials` lists the `Write`; tool result: "`...h1b.txt` is outside `...orch-probe`; --restricted confines the file tools to the working directory." |
| 2 | H2: worktree `SessionStart` hook | `h2.txt` absent | **Pass.** `h2.txt` did not appear; the call answered PONG with a valid structured report. The `.claude/settings.json` was ignored. |
| 3 | Read of a dummy file outside the worktree | read denied | **Pass.** `permission_denials` lists the `Read`; same "confines the file tools" message. No contents returned. (Only a dummy file was used, never `~/.claude` or any real credential.) |
| 4 | Builder still works under `--restricted` | Bash ran the exact test command; `structured_output` present | **Pass.** With the exact rule `Bash(python -m pytest -q)`, Haiku ran it ("1 passed in 0.02s"), `permission_denials` empty, `structured_output` present, `blocked: false`. `--restricted` did not remove `Bash` because `--tools` names it, and `--json-schema` works with it. |
| 5 | M6 write: `agy --mode plan --sandbox` | `m6b.txt` absent | **Pass, with a caveat.** `m6b.txt` was not created and nothing changed in the repo. agy did not try to write the file: in plan mode it wrote its own `implementation_plan.md` artifact under `~/.gemini/antigravity-cli/brain/<conversation>/` and asked for approval. So plan mode defers the write rather than denying it. |

## What this means

- The worker flags now confine the file tools to the working directory (H1) and ignore user, project and local settings,
  hooks and MCP servers (H2). The builder keeps `Bash` for the exact test command only.
- agy: no write was possible in plan mode in either probe. The mode asks for approval instead of writing, and agy's own
  artifact folder under `~/.gemini` is outside orch's control. `--sandbox` accepted the flags without error, but the
  probe did not show what it adds on its own.
- Not probed: `--sandbox` together with `--add-dir` (the agy reviewer uses both), and the planner and reviewer Claude
  roles under `--restricted` (they only lose settings and MCP; their toolset was already `Read,Grep,Glob`).
- Still true (C1): the allowed test command runs code the agent wrote with the user's full rights. These flags confine the
  Claude tools, not the test process. A real sandbox is still needed.
- Each Claude probe cost about 10k tokens of plan quota; the agy probes about 14k to 20k.
