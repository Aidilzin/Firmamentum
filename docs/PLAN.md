# AI Orchestrator Plan: Claude Pro + Gemini Pro

Nov 10, 2026 · @Aidil

## Summary

Build a small Python orchestrator that runs on your laptop and drives the **official** coding CLIs as worker agents: Claude Code (`claude -p`) on your Claude Pro plan and Antigravity CLI (`agy -p`) on your Google AI Pro plan. The orchestrator never touches login tokens or private endpoints. It only spawns the CLIs, passes them a task, and reads their JSON output.

This matters because both vendors have closed the "use my subscription inside my own app" route. Anthropic does not allow Claude Pro login tokens in third-party tools, and Google moved Pro users off Gemini CLI to Antigravity CLI in June 2026. Driving the official CLIs in headless mode is the route both vendors document, so it is the one that will keep working.

**What you get at the end:** one command such as `orch run "add password reset to the booking app"` that plans the task, splits it into steps, gives each step to the agent best suited for it, has the other agent review the result, runs your tests, and shows you a diff to approve.

**Good news:** your Firmamentum design (Laplace spec, Maxwell build, Descartes repair, rich terminal UI, pytest loop) maps almost directly onto this. Treat this project as Firmamentum v2: keep the personas and the loop, and replace the RunPod Qwen backend with CLI adapters. That removes the GPU pod cost completely.

## Status (updated Nov 10, 2026)

**Phases 0-5 are done** (see `docs/phase0-findings.md` ... `docs/phase5b-notes.md`). Not built by design: web UI, vector DB, auto-merge to main. Not built yet: MCP server (optional item of Phase 5).

| Phase | Result |
| --- | --- |
| 0 Spike | both CLIs run headless from Python on Windows, fixtures saved |
| 1 Adapters | `AgentAdapter`, Claude/Antigravity/Fake adapters, usage log in SQLite |
| 2 Planner | `orch plan`, schema-validated plan, scout optional |
| 3 Execution | worktrees, builder, orchestrator-run tests, path guards |
| 4 Review/repair/approval | reviewer by schema, repair rounds, `approve/reject/answer/merge` |
| 5a Router | fallback lists per role, cooldowns, reset-time parsing, `resume` pauses |
| 5b UX | rich live view, `--plain`, practice repo, `--hidden-tests`, README |

### Decisions that changed from this plan

- **agy takes the prompt as `-p=<prompt>`** (one argv item), and **agy does not read stdin**. claude gets the prompt on stdin. Prompts over 8000 chars go through a temp file under `.orch/prompts/`.
- **Structured output** comes back in a top-level `structured_output` key from both CLIs (`--json-schema` inline, `$defs` accepted). Pydantic still validates; one retry.
- **No `--max-turns`** in this claude version; the turn cap is a 20 min timeout that kills the process tree. `--max-budget-usd` is not used (meaningless on a subscription).
- **Roles are ordered lists** (`claude:opus, claude:sonnet, agy:default`), not `{agent, model}` plus `fallback_model`. builder and repair are claude-only because agy has no tool allow-list.
- **Router**: one fallback hop per call, SQLite `cooldowns` table, reset-time parsing, `orch resume` waits for `resume_after`.
- **Test command comes from `config.yaml` only** (plus optional `retest_command`); the plan's `test_command` is ignored with a warning.
- **Worktrees live outside the repo** (`%LOCALAPPDATA%\orch\wt`) so tests in the main checkout cannot collect them; every destructive git call is path-guarded.
- **Approvals are per file and rule**, not per step; `tests_removed` and `all_agents_rate_limited` were added to the approval list; repairs-exhausted needs `approve --retry`, never a plain approve.
- **Repair reads a copied input folder** (`repair_input/roundN`), not the step folder; it tries `--resume` of the builder session and falls back to a fresh call.
- **Reviewer** is read-only claude (`--tools Read,Grep,Glob`) or agy `--mode plan`; blocker/major <=> reject, minor never blocks.
- Usage log is one global SQLite file because plan quota is shared across projects.
- `VIRTUAL_ENV` is removed from every child environment (uv warning noise).
- Gemini model names are not pinned: `agy:default` means no `--model` flag.

### Next ideas

- Capture real rate-limit messages and tighten `RATE_LIMIT_PATTERNS` / reset parsing; add `tzdata` for exact zone names (needs your OK).
- MCP server so Claude Code or Antigravity can call orch.
- Parallel independent steps (two worktrees), after the sequential flow has more real mileage.
- `orch clean --all` for old worktrees and branches; desktop notification on stops.
- A second target repo (Laravel/React) to exercise non-Python `test_command`s and `tests_removed` for other languages.
- Use agy as a real reviewer and compare verdicts with claude's.

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

1. You type `orch run "<task>"`. The Engine creates a run folder and saves state after every step.
2. The Planner asks one agent for a JSON plan (steps, dependencies, files, test command).
3. The Router gives each step a role and an agent, and checks the budget first.
4. The Workspace makes a git worktree for the step. The Builder agent edits files there.
5. The Test runner runs your tests. The other agent reviews the diff.
6. Fail goes to Repair (max 3 rounds). Pass goes to you as a diff to approve and merge.

**Starting role map** (a default to tune from your own usage logs, not a fixed rule):

| Role | Firmamentum name | Default agent | Default model | Tools allowed |
| --- | --- | --- | --- | --- |
| Planner | Laplace | Claude Code, plan mode | Opus | Read only |
| Scout | new | Antigravity | Gemini 3.8 Flash (or Gemini 4 Argon for big repos) | Read only, summarises a large codebase for the Planner |
| Builder | Maxwell | Claude Code | Sonnet | Edit and run, inside the worktree only |
| Reviewer | new | Claude Code | Opus | Read only, returns verdict by schema |
| Repair | Descartes | Claude Code | Sonnet (same as Builder) | Edit and run, given the failing test output |

**Changing models.** Every role reads its agent and model from `config.yaml`, and the adapter passes it straight to the CLI's own `--model` flag (`claude --model opus`, `agy --model <name>`). So any model the CLI accepts works, with no code change. You can also override per run, for example `orch run "..." --model builder=opus --model scout=argon`, and `orch models` lists what each CLI currently offers.

```yaml
roles:
  planner:  { agent: claude, model: opus }
  scout:    { agent: agy,    model: gemini-3.8-flash }
  builder:  { agent: claude, model: sonnet }
  reviewer: { agent: claude, model: opus }
  repair:   { agent: claude, model: sonnet }
fallback_model:
  claude: sonnet   # used when Opus limits run out
```

Note: Opus uses Pro limits much faster than Sonnet, so the Router should drop the Planner and Reviewer to `fallback_model` when it sees a limit warning instead of stopping the run. Check the exact Gemini model names with `agy models` during Phase 0.

With this setup Gemini mainly saves Claude usage by doing the heavy reading. Opus still reviews Sonnet's work, so the review comes from a stronger model than the builder. If you later want a second opinion from a different model family, set the Reviewer to \`agy\` in config.

## Roadmap

Build in six phases. Each phase ends with something you can run, so you can stop at any phase and still have a useful tool. Rough effort assumes evenings and weekends alongside your internship.

1. **Phase 0: Spike (1 weekend).** Prove both CLIs run headless from Python on your Windows laptop.
   - Call `claude -p "Reply PONG" --output-format json` and `agy -p "Reply PONG" --output-format json` through `subprocess`, with stdout piped.
   - Save one real JSON reply from each to `fixtures/`. These become your parser tests.
   - Check the known Antigravity issue where `-p` printed nothing when stdout was not a terminal. If it still happens, try `stream-json` or run agy inside WSL.
   - **Done when:** one script prints both replies and the token usage of each.
2. **Phase 1: Adapters (1 to 2 weekends).** One `AgentAdapter` interface with `ClaudeCodeAdapter`, `AntigravityAdapter` and a `FakeAdapter` for tests.
   - Each adapter handles: building the command, passing the prompt by stdin or temp file (avoids Windows quoting bugs), timeout, parsing JSON, detecting "limit reached" errors, and logging usage to SQLite.
   - **Done when:** `orch ask --agent claude "explain this repo"` and `--agent gemini` both work, and unit tests pass using only the fake adapter.
3. **Phase 2: Planner and task graph (2 weekends).** The Planner role (your Laplace) turns a request into a JSON plan: steps, dependencies, files touched, test command, and which role does each step.
   - Force the shape with `--json-schema` so you never parse prose.
   - Save the plan to `.orch/runs/<id>/plan.json` so a run can resume after a crash or a usage limit.
   - **Done when:** `orch plan "..."` writes a valid plan for a real task in one of your repos.
4. **Phase 3: Isolated execution (2 weekends).** Each step runs in its own `git worktree` on its own branch, so two agents never edit the same files at once and a bad step is thrown away with one command.
   - After the Builder (Maxwell) finishes, the orchestrator runs the test command itself. Do not trust an agent's claim that tests passed.
   - **Done when:** a two-step task in a toy repo finishes on a branch with green tests.
5. **Phase 4: Review, repair and approval (2 weekends).** The Reviewer (Opus by default) checks each diff and returns `{verdict, issues[]}` by schema. Failed tests or a "reject" verdict go to the Repair role (Descartes), maximum 3 rounds. Anything on the approval list (see Decisions) pauses for you; everything else continues on its own.
   - **Done when:** a deliberately broken step is caught, repaired, and shown to you as a clean diff.
6. **Phase 5: Router, budget and UX (ongoing).** Routing rules in `config.yaml`, a quota tracker that shifts work to the other agent when one hits its limit, a `rich` live view, `orch status`, `orch resume`. Optional: expose the orchestrator as an MCP server so you can call it from inside Claude Code or Antigravity.

**Do not build in v1:** a web UI, a vector database, parallel runs of more than two agents, or auto-merge to main. Your Firmamentum notes already show the benefit of staying lightweight.

## Building it with Claude Code

Work one phase per Claude Code session, start each in plan mode, and keep the plan and rules in files Claude Code reads every time. Remember that building with Claude Code and running the orchestrator both spend the same Pro limits, so test with the fake adapter and only make real CLI calls when you need to.

**Repo layout (start here):**

```
orch/
  CLAUDE.md            rules for Claude Code
  docs/PLAN.md         this plan, exported as Markdown
  orch/
    cli.py             typer commands: ask, plan, run, status, resume
    adapters/          base.py, claude_code.py, antigravity.py, fake.py
    roles/             planner.md, builder.md, reviewer.md, repair.md
    schemas/           plan.json, review.json
    engine.py          task graph, state machine, retries
    workspace.py       git worktree create, diff, cleanup
    budget.py          usage log (SQLite), limit detection, cooldown
    ui.py              rich live view
  tests/  fixtures/
  config.yaml          routing rules and model choices
```

**Starter CLAUDE.md:**

```markdown
# orch: personal multi-agent orchestrator

## What this is
A Python CLI that drives the official Claude Code (`claude -p`) and
Antigravity (`agy -p`) CLIs as worker agents. See docs/PLAN.md.

## Hard rules
- Never read, copy or call with OAuth tokens. Only spawn the official CLIs.
- Unset ANTHROPIC_API_KEY in the child env before spawning `claude`.
- Unit tests use FakeAdapter only. Never call a real CLI in tests.
- Every agent call goes through budget.py so usage is logged.
- Agents work in git worktrees under .orch/wt/, never in the main checkout.
- Before any merge, delete, migration or new dependency, check approval_rules
  in config.yaml and pause the run if one matches.
- No local models. Only the claude and agy CLIs.

## Stack
Python 3.12, uv, typer, pydantic, rich, pytest, sqlite3. No LangChain.

## Commands
- Tests: `uv run pytest -q`
- Lint: `uv run ruff check .`

## Working style
- Small commits, one feature each. Write the test first.
- Ask before adding a new dependency.
```

**Kickoff prompts, one per session:**

1. *Phase 0:* "Read docs/PLAN.md. Write `spike.py` that runs `claude -p` and `agy -p` with JSON output through subprocess on Windows, prints both results and usage, and saves the raw JSON to fixtures/. Do not build anything else yet."
2. *Phase 1:* "Using the fixtures, design the `AgentAdapter` interface and a `Result` pydantic model. Show me the interface first. Then implement FakeAdapter and tests, then the two real adapters."
3. *Phase 2:* "Write `schemas/plan.json` and `roles/planner.md`, then `orch plan`. The plan must validate against the schema before it is saved."
4. *Phase 3 onward:* "Implement the next roadmap phase. Start in plan mode, list the files you will touch, and wait for my OK."

Useful habits: run `/clear` between phases, ask Claude Code to update docs/PLAN.md when a decision changes, and commit after each green test run. If you want, the existing Firmamentum persona files (`laplace_spec.md`, `maxwell_build.md`, `descartes_repair.md`) can be copied straight into `roles/`.

## Risks and open questions

| Risk | Effect | What to do |
| --- | --- | --- |
| Usage limits hit mid-run | Run stops halfway | Save state after every step, detect the limit message, pause and resume later or hand the step to the other agent |
| Vendor terms change again | A backend stops working | Keep both behind the adapter interface; keep an API-key backend option |
| Antigravity CLI is new (v1.x) | Flags or output shape change | Pin the version you tested, keep fixture tests, check release notes before upgrading |
| Agent edits the wrong files or runs risky commands | Broken repo | Worktrees only, `--allowedTools` allow-lists, no skip-permissions outside a sandbox, human approval before merge |
| Windows subprocess quirks | Empty output, broken quoting | Pass prompts by stdin or file; fall back to WSL if needed |
| Two agents agree on a wrong answer | False confidence | Tests decide, not the reviewer's verdict |

**Decisions (10 Nov 2026):**

- **First target repo:** the orchestrator itself (Python, tested with `uv run pytest -q`). The test command is stored per repo in `config.yaml` so a Laravel/React repo works later with `php artisan test` or `npm test`.
- **Run mode:** fully in the background. It pauses only for sensitive or critical actions, listed below.
- **Local model:** dropped for now. Only the paid Claude Pro and Google AI Pro accounts are used.

Reply: What do you mean by this question because I don't understand?

Reply: It should run fully in the background and only pause for my approval on sensitive/critical/important questions or task before proceeding.

Reply: For the moment, drop all local model support for the time being. We will only be using my paid account for Claude and Gemini.

**When the orchestrator stops and asks you** (everything else runs on its own):

| Trigger | Why it pauses |
| --- | --- |
| Merge or push to your main branch | Hard to undo |
| Deleting files outside the step's worktree | Risk of data loss |
| Changes to auth, payments, secrets or `.env` files | Security |
| Database migrations or schema changes | Can break real data |
| Adding a new dependency | Supply-chain and licence risk |
| Tests still failing after 3 repair rounds | Needs a human call |
| The Planner marks a step as unclear (`needs_input: true`) | Guessing wastes usage |
| Both agents have hit their usage limit | Nothing left to route to |

How to build it: keep this list in `config.yaml` as `approval_rules`, add `needs_input` and `question` fields to the plan schema, and let the Engine pause the run, send you a desktop notification, and continue after `orch approve <run> <step>`.

**Next step:** install Antigravity CLI, log in to both CLIs once by hand, then open Claude Code in an empty `orch/` folder with the starter CLAUDE.md and run the Phase 0 prompt.
