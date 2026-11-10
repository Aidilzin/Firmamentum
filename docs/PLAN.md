# Firmamentum v2: Plan and Status

Nov 11, 2026 · @Aidil

## Status

Firmamentum v2 is built and working. Phases 0 to 5b are done on branch `v2` of [github.com/Aidilzin/Firmamentum](https://github.com/Aidilzin/Firmamentum), with 255 tests that never call a real CLI and real end-to-end runs on two repos. The old Qwen/RunPod version is kept as tag `v1-qwen-runpod`.

**What it does:** `orch run "<task>"` plans the task (Laplace, Opus), builds each step in an isolated git worktree (Maxwell, Sonnet), runs your tests itself, reviews the change (Loschmidt, Opus), repairs failures for up to 3 rounds (Descartes, Sonnet), and commits. It stops and asks you before anything risky, and only `orch merge` (run by you) touches your branch. It never pushes.

**How it stays within the rules:** it only spawns the official `claude` and `agy` CLIs on your Claude Pro and Google AI Pro logins. It never touches login tokens.

| Proven in real runs | Tested with fakes only |
| --- | --- |
| Plan, build, test, review, commit | Fallback routing and cooldowns (no limit was hit) |
| Repair resuming Maxwell's session (`--resume`) | Real rate-limit message wording and reset times |
| Hidden tests forcing a real repair round | Brillouin (scout) and agy as fallback reviewer |
| `tests_removed` stop, before and after the build, with per-file approval | Multi-step plans with `--hidden-tests` |
| `Bash(rm *)` denied under `--permission-prompts none` | Non-Windows systems |
| `orch merge` refusing `main` without `--allow-main` |  |

**Real cost per run** (tokens, mostly cache):

| Role | Practice A (repair) | Practice B (approval) |
| --- | --- | --- |
| Laplace, planner (Opus) | 44,761 | 44,840 |
| Maxwell, builder (Sonnet) | 74,019 | 74,113 |
| Descartes, repair (Sonnet) | 71,413 | 0 |
| Loschmidt, reviewer (Opus) | 56,640 | 26,703 |
| **Total** | **246,833** | **145,656** |

On Claude Pro, one small Sonnet build measured about 3% of the 5-hour window, so a full run with review is roughly 5 to 10%. Building the tool interactively in Claude Code cost far more than running it.

**Next:** Phase 6, the UI/UX pass (see Roadmap).

## Ground rules for your two subscriptions

The single rule: **call the vendor's own CLI, never reuse its login token in your own code.** Everything else follows from that.

|  | Claude Pro | Google AI Pro |
| --- | --- | --- |
| Worker tool | Claude Code CLI, `claude -p` | Antigravity CLI, `agy -p` (replaced Gemini CLI for Pro users on 18 June 2026) |
| Allowed | Your script running `claude -p` while logged in with your plan | Your script running `agy -p` while logged in with your plan |
| Not allowed | Copying the OAuth token into your own HTTP client, Agent SDK, or tools like OpenCode or Cline | Using old Gemini CLI with "Login with Google" (no longer served for Pro) |
| Billing for headless runs | Counts against your normal Pro limits (5 hour window plus weekly cap), shared with chat and interactive Claude Code | Uses your Pro plan's built-in Antigravity quota, shared with the Antigravity IDE you already use |
| Structured output | `--output-format json` or `stream-json`, `--json-schema` | `--output-format json` or `stream-json`, `--json-schema` |
| Unattended approvals | `--permission-mode`, `--allowedTools` | `--mode accept-edits`, or `--dangerously-skip-permissions` inside a sandbox only |
| Resume a session | `--resume <id>` / `--continue` | `--conversation <id>` / `--continue` |

Three practical consequences for the design:

- **Budget is the real limit.** Pro on both sides is sized for one person's normal use. The orchestrator must count calls, back off when a limit hits, and prefer the cheaper model (Sonnet or Haiku, Gemini Flash) for routine steps.
- **Do not set `ANTHROPIC_API_KEY`.** If that variable exists, Claude Code bills the API key instead of your Pro plan. Your adapter should unset it before spawning `claude`.
- **Keep an API-key escape hatch.** If you later want heavy unattended runs, the clean path is a paid API key, not a workaround. Design the adapter layer so a backend can be swapped without touching the rest.

Sources: [Claude Help Center, Agent SDK with your plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) (Oct 2026 update: `claude -p` still draws from subscription limits) · [Winbuzzer on the OAuth ban](https://winbuzzer.com/2026/02/19/anthropic-bans-claude-subscription-oauth-in-third-party-apps-xcxwbn/) · [Gemini CLI transition notice](https://github.com/google-gemini/gemini-cli/discussions/28017) · [Antigravity CLI headless docs](https://antigravity.google/docs/cli/headless/) · [Antigravity CLI flag reference](https://antigravity.google/docs/cli/reference/)

## Architecture

The orchestrator is the boss and the two CLIs are workers. All vendor-specific code lives in the adapters, so the rest of the system does not care which model does a step.

&#91;embedded content: orchestrator architecture · 6 core parts, 2 CLI workers\]

One run moves through these steps:

1. `orch run "<task>"` creates a run folder in `<repo>/.orch/runs/<id>/` and saves state after every change, so it can resume after a crash or a usage limit.
2. Optional: Brillouin (scout) writes a short repo summary.
3. Laplace (planner) returns a JSON plan, enforced with `--json-schema` and checked again with pydantic. Steps with risk flags stop for your approval before any build.
4. A git worktree is created outside the repo (`%LOCALAPPDATA%\orch\wt\...`) on branch `orch/<id>`. Maxwell (builder) does one step at a time there.
5. orch checks the real changed files from git (protected paths, dependency files, removed tests), then runs the test command from `config.yaml` itself.
6. Loschmidt (reviewer) reads the diff. A test failure or a reject sends the step to Descartes (repair), up to 3 rounds.
7. Pass and approve: orch commits the step. When all steps are done you check `orch diff` and run `orch merge`.

**The five demons**

| Role | Demon | Default entries, in fallback order | Tools | Why the name fits |
| --- | --- | --- | --- | --- |
| Planner | Laplace | claude:opus, claude:sonnet | Read, Grep, Glob | Laplace's demon knows the whole state and can predict the future |
| Scout | Brillouin | agy:default | agy `--mode plan` | Brillouin showed Maxwell's demon must first observe before it can act |
| Builder | Maxwell | claude:sonnet | Read, Grep, Glob, Edit, Write, Bash (test command only) | Maxwell's demon does the sorting work |
| Reviewer | Loschmidt | claude:opus, claude:sonnet, agy:default | Read, Grep, Glob | Loschmidt's paradox runs the process backwards to check it |
| Repair | Descartes | claude:sonnet (resumes Maxwell's session) | same as builder | Descartes' demon is about doubt and deception: assume it is wrong and fix it |

**Config** (`config.yaml`, the only source of models and test commands):

```yaml
roles:
  planner:  [claude:opus, claude:sonnet]
  scout:    [agy:default]
  builder:  [claude:sonnet]
  reviewer: [claude:opus, claude:sonnet, agy:default]
  repair:   [claude:sonnet]
cooldown_minutes: 60
review: per_step
max_repair_rounds: 3
repos:
  orch:     { path: ".", test_command: "uv run pytest -q", retest_command: "uv run pytest -q --lf" }
  practice: { path: "../orch-practice", test_command: "uv run pytest -q", retest_command: "uv run pytest -q --lf" }
```

**Routing and cooldowns:** each role tries its entries in order. A rate limit on Claude cools all Claude models (the Pro limit is per account), unless the message names exactly the model that failed. A call gets at most one fallback hop; if nothing is available the run pauses with a resume time. Override per run with `--model role=model`, inspect with `orch cooldowns`.

## Roadmap

Phases 0 to 5b are done. Phase 6 is the UI/UX pass. Every phase followed the same loop: plan mode, approve with amendments, fake tests, then one real check with your OK.

| Phase | What it added | Status |
| --- | --- | --- |
| 0 | Headless spike for `claude -p` and `agy -p=` on Windows | Done |
| 1 | Adapters, UTF-8 process runner, global usage log, `orch ask` | Done |
| 2 | Laplace, plan schema, run storage, `orch plan` | Done |
| 3 | Maxwell, worktrees, `orch exec/run/resume` | Done, built `orch version` itself |
| 4 | Loschmidt, Descartes, `approve/reject/answer/merge` | Done, built `orch usage --json` itself |
| 5a | Router, fallback lists, cooldowns | Done (fakes only) |
| 5b | Practice repo, `--hidden-tests`, rich live view, README | Done |
| 6 | UI/UX: a polished, distinct CLI | Next |

## Phase 6: UI/UX direction

Goal: feel as polished as Claude Code or Antigravity CLI, but with its own identity. The name gives the theme: the firmament is the night sky, and the five demons are its constellation.

**Visual identity**

- Night-sky palette: deep indigo background tones, starlight white text, one accent colour per demon, used everywhere that demon appears.
- Star glyphs instead of emoji (`✦ ✧ ⋆ ·`), with an ASCII fallback for old consoles and `NO_COLOR`.
- Steps drawn as a constellation: `✦───✦───✧───·` (done, done, running, pending), not a plain table.
- A small wordmark on start, shown once, with the active models and today's token use.

**Interaction**

- `firm` with no arguments opens an interactive shell like Claude Code: a prompt, slash commands (`/run`, `/plan`, `/status`, `/approve`, `/diff`, `/usage`, `/cooldowns`, `/model`), history and tab completion. One-shot commands keep working for scripts.
- Inline output that stays in your terminal scrollback (prompt\_toolkit for input, rich for output), not a full-screen app. This matches how Claude Code feels.
- Approvals inline: `[a]pprove  [r]eject  [d]iff  [q]uit`, with a syntax-highlighted diff viewer.
- Live demon activity: switch the adapters to `stream-json` so you see "Maxwell ▸ Edit src/practice/text.py" as it happens, instead of a silent wait.
- `firm doctor`: checks both CLIs, PATH, Pro login (not Console), leftover `ANTHROPIC_*` or Ollama settings, and `uv`. It catches every setup problem hit in this project.
- `firm init`: a guided setup that writes `config.yaml` for a new repo.

**Sub-phases**

1. 6a: identity, theme module, wordmark, constellation progress, `firm doctor`.
2. 6b: interactive shell with slash commands.
3. 6c: streaming demon activity (`stream-json` parsing in both adapters).
4. 6d: inline approvals, diff viewer, `firm init`, Windows toast when a run stops.

**Decisions (11 Nov 2026)**

- Command name stays `orch` (a play on "arch").
- Inline shell for now; a full-screen app may come later, so 6a adds a view layer that a full-screen front end could reuse.
- Night-sky palette, one colour and glyph per demon, dark theme by default plus a light theme:

| Use | Glyph | Dark theme | Light theme |
| --- | --- | --- | --- |
| Laplace, planner (also the `orch` wordmark) | ✦ | #7AA2F7 | #2E5EAA |
| Brillouin, scout | ◎ | #7DCFFF | #0E7C86 |
| Maxwell, builder | ◆ | #FF9E64 | #B85C00 |
| Loschmidt, reviewer | ↺ | #BB9AF7 | #7A4FC2 |
| Descartes, repair | ✚ | #F38BA8 | #B4235A |
| Success | ✓ | #9ECE6A | #3F7D20 |
| Needs you / warning | ! | #E5C07B | #9A6B00 |
| Error | ✗ | #FF6B6B | #C62828 |
| Text / dim text |  | #E6E9F5 / #8A90B0 | #1F2340 / #5E6385 |
| Constellation line | ─ | #3B4266 | #C9CCE0 |

The terminal keeps its own background; orch never paints one.

**Later ideas:** use it on the ISP550 booking system (`php artisan test`), an MCP server so Claude Code or Antigravity can call orch, parallel steps, capture the first real rate-limit message, non-Windows support, auto-clean old worktrees.

## How it was built

One phase per Claude Code session (`/clear` between them), always starting in plan mode. The habits that worked:

- Review Claude Code's plan before approving, and approve with a short list of amendments. Most real bugs were caught here, not in testing.
- Unit tests use fakes only. Real CLI calls happen once per phase, at the end, after an explicit OK and a `/usage` check.
- Save raw CLI output to a file before printing anything.
- Commit locally, ask before pushing, never push to `main`.

**Repo layout (as built)**

```
orch/
  CLAUDE.md  config.yaml  README.md
  docs/            PLAN.md, phase0..phase5b notes, real-run logs
  src/orch/
    adapters/      base, process, claude_code, antigravity, fake
    budget.py      global usage log + cooldowns (SQLite)
    router.py      fallback lists, one hop per call
    config.py      roles, repos, approval rules
    plan.py  planner.py  build.py  review.py
    executor.py    step state machine
    workspace.py   worktrees + path guard
    runs.py  ui.py  versions.py  cli.py
    roles/         planner, builder, reviewer, repair (.md)
    schemas/       plan, build, review (.json)
  scripts/         make_practice_repo.py
  tests/  fixtures/
```

**Windows lessons**

- Paste one PowerShell command at a time: pasted lines joined together twice.
- `~/.claude/settings.json` had leftover Ollama settings (`ANTHROPIC_BASE_URL`, `qwen3:8b`). Claude Code also first logged in through a Console account ("Credit balance is too low"); use the Pro subscription login.
- agy needs `-p=<prompt>` as the last argument and cannot read stdin. Claude reads the prompt from stdin.
- Write stdin as UTF-8 bytes (no `\r\n`), decode output as UTF-8, and remove `VIRTUAL_ENV` from child processes.
- `tzdata` is needed for time zone names like `Asia/Singapore`.

## Safety model and decisions

Runs go on in the background and stop only for these. Each stop prints the exact command to run next.

| Stop | When | What you run |
| --- | --- | --- |
| Plan risk flag | Laplace flags a step (auth, migration, new dependency, tests removed) | `orch approve <run> --step sN` |
| Post-check | Real changed files hit a protected path, `pyproject.toml`/`uv.lock`, or remove tests. Approval is per file | `orch diff <run>` then `orch approve` |
| Repairs used up | Tests still fail or review still rejects after 3 rounds. Never committed | `orch approve --retry` or `orch reject` |
| Needs input | Laplace or a demon is blocked by an unclear request | `orch answer <run> "..."` |
| Paused | Every entry for a role is cooling down | `orch resume <run>` after the shown time |
| Merge | Always yours. Refuses `main` without `--allow-main`, then runs the full tests | `orch merge <run>` |

Other guards: worktrees live outside the repo; the test command comes only from `config.yaml`, never from a plan; destructive git commands check the path is a linked orch worktree; Maxwell and Descartes cannot run `git`, `uv add`, `pip`, `rm` or `del`; Loschmidt and Laplace are read-only; orch never pushes.

**Decisions that changed from the first plan**

| First plan | As built | Why |
| --- | --- | --- |
| Gemini CLI | Antigravity CLI (`agy`) | Gemini CLI stopped serving Google AI Pro on 18 June 2026 |
| Prompt by stdin for both | Claude stdin, agy `-p=<prompt>` last | agy cannot read stdin and swallows the next token after `-p` |
| Parse JSON from the reply text | Read `structured_output` | Both CLIs return the schema result there |
| Turn cap with `--max-turns` | 20-minute timeout | Flag does not exist in this Claude Code version |
| Approve by rule | Approve by file and rule | A later repair could otherwise touch a new protected file |
| One model per role | Fallback lists with cooldowns | Keeps runs going when one limit is hit |
| Local Qwen via Ollama | Dropped | Only the two paid subscriptions are used |
| First target: any repo | orch itself, then the practice repo | Cheap, safe self-tests |

**Still untested for real:** rate-limit message wording and reset formats, Brillouin and agy as a fallback reviewer, multi-step `--hidden-tests`, non-Windows.
