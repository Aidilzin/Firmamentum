# Brief for planning Phase 6b: the interactive `orch` shell

Paste this whole file to Claude Opus on the web and ask it to produce a **plan** for Phase 6b (not code). You have no access to my repo, so everything you need is below. Ask me questions only where the answer would change the plan.

## 1. What the project is
`orch` is my personal Python 3.12 CLI (Windows 11, PowerShell, uv, pytest, git) that orchestrates two official coding CLIs as worker agents: Claude Code (`claude -p`, Claude Pro login) and Antigravity (`agy -p`, Google AI Pro login). It never touches OAuth tokens; it only spawns the official executables. The product name is **Firmamentum**; the command is `orch`.

Five "demons" (roles) do the work, each with a name, glyph and colour:

| role key | demon | glyph | ASCII | dark | light |
|---|---|---|---|---|---|
| planner | Laplace | ✦ | `*` | #7AA2F7 | #2E5EAA |
| scout (opt-in) | Brillouin | ◎ | `o` | #7DCFFF | #0E7C86 |
| builder | Maxwell | ◆ | `#` | #FF9E64 | #B85C00 |
| reviewer | Loschmidt | ↺ | `<` | #BB9AF7 | #7A4FC2 |
| repair | Descartes | ✚ | `+` | #F38BA8 | #B4235A |

Flow of `orch run "<task>"`: create a run folder `<repo>/.orch/runs/<id>/`, Laplace returns a JSON plan (schema-checked), a git worktree is created outside the repo, Maxwell builds one step at a time, orch itself checks the changed files and runs the repo's `test_command`, Loschmidt reviews the diff, a failing test or reject goes to Descartes (up to 3 repair rounds), risky steps stop for my approval, each passing step is committed, and at the end I run `orch diff` then `orch merge`. State is saved to `meta.json` after every change, so a run survives a crash or a usage limit (`orch resume`).

## 2. Current commands (one-shot, Typer)
`ask, usage, cooldowns, version, doctor, plan, runs, show, exec, run, resume, status, approve, reject, answer, diff, merge, clean`. Run ids can be a unique prefix. `--repo <name-or-path>` (default cwd). `--model role=model` overrides. `--plain` forces line output. Exit codes of exec/run/resume/approve/answer: 0 done, 1 failed, 2 needs input/approval/refused, 3 paused. Global options before the subcommand: `--theme dark|light`, `--ascii`. `orch` with no args prints a home screen and exits.

## 3. What Phase 6a built (done, committed, 300 tests passing)
- `theme.py`: palettes, one demon registry (`DEMONS`), `resolve_theme` (`--theme` > `ORCH_THEME` > config `ui.theme` > dark), `NO_COLOR` support, ASCII fallback. No background colour is ever set.
- `view.py`: a **pure** `RunView` built from `meta.json` (`build_run_view(meta, first_entries, elapsed, ...)`). Fields: run id, status, models, steps (id, title, status, current demon, phase, round, tokens, elapsed), current activity, totals, per-demon lines (text, model, tokens/time, running), and `StopInfo` (what, why, next commands, border kind). No rich, no printing.
- `ui.py`: renderers over `RunView`: `render_rich` (constellation `✦───✦───✧───·  step 3 of 4 · running · title`, then one line per demon like `◆ Maxwell     building s3     sonnet · 0:41`, then a footer), `render_plain`, `render_stop_panel` (warning-colour border for "needs you", error colour for failures), `render_doctor`. `PlainView` and `LiveView` implement the executor's display interface.
- `home.py`: the no-arg screen (wordmark, repos, first model per demon, tokens last 5h/24h, cooldowns, latest run, example commands).
- `doctor.py`: 17 checks with ✓/!/✗ and a one-line fix; `--live` makes one "Reply PONG" call per CLI after confirmation.
- The executor now writes `started_at`/`ended_at` (UTC) per step and per run in `meta.json`.

**Executor interface (unchanged and must stay so):** `Executor(repo, run_dir, cfg, get_adapter, log=callable(str), on_update=callable(meta_dict), hidden_tests=None)`; methods `start()`, `resume()`, `approve(step_id, retry)`, `reject(step_id)`, `answer(step_id, text)` each **block** until the run finishes or stops and return `ExecOutcome(status, step_id, message)`. `on_update(meta)` fires after every save. A run can take several minutes and a real call costs ~13k–20k tokens of plan quota just for system prompts.

## 4. What I want in 6b
From my roadmap (`docs/PLAN.md`): "`orch` with no arguments opens an interactive shell like Claude Code: a prompt, slash commands (`/run`, `/plan`, `/status`, `/approve`, `/diff`, `/usage`, `/cooldowns`, `/model`), history and tab completion. One-shot commands keep working for scripts. Inline output that stays in terminal scrollback (prompt_toolkit for input, rich for output), not a full-screen app." Later sub-phases: 6c streaming demon activity (`stream-json` in both adapters), 6d inline approvals `[a]pprove [r]eject [d]iff [q]uit` with a syntax-highlighted diff viewer, `orch init`, Windows toast when a run stops. A full-screen app may come much later; the `RunView` layer exists so it could reuse the data.

**My complaint that triggered this phase:** when I launch `claude` or `agy` I get an interactive environment with a prompt that stays open. `orch` today prints a screen and exits to PowerShell. I want to type `orch` once and *stay inside orch*.

## 5. Constraints
- Do not read or copy any credential/OAuth file; only spawn `claude` and `agy`. Remove `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` from the `claude` child env.
- **Ask before adding any dependency** beyond the standard library and pytest. Currently in: typer, rich, pydantic, pyyaml, tzdata. `prompt_toolkit` would be new, so the plan must justify it, compare it with alternatives (stdlib `readline`/`pyreadline3`, rich-only input), and say plainly what it costs.
- Tests never call the real CLIs (fake adapters). Keep real calls to a minimum.
- Inline output only (scrollback preserved), Windows Terminal + PowerShell first; must degrade to plain when stdout is not a tty.
- Keep `RunView` pure and renderers separate; do not change executor logic beyond small data additions.
- Small commits; each phase is planned first (plan mode), approved with amendments, built with fakes, then one real check.

## 6. What I want back from you (the plan)
1. **Architecture**: how the shell loop sits on top of the existing one-shot commands without duplicating them (e.g. a command registry shared by Typer and slash commands), where session state lives (current repo, current run, theme, model overrides), and module/file layout.
2. **Blocking executor vs. prompt**: runs block for minutes. Options: run the executor in a worker thread while the prompt stays usable (cancel, `/status`, approve), or keep it blocking with Ctrl-C handling. Recommend one, explain how `on_update` feeds the live constellation without corrupting the prompt, and how Ctrl-C/Esc should behave (stop the run safely, never leave a worktree half-written).
3. **Slash command set** with exact syntax, arguments and defaults (`/run`, `/plan`, `/status`, `/approve`, `/reject`, `/answer`, `/diff`, `/merge`, `/usage`, `/cooldowns`, `/model`, `/repo`, `/theme`, `/doctor`, `/help`, `/exit`, and whatever else you think is needed), including how free text typed without a slash should behave (probably "plan and run this task" with a confirmation, because every run costs quota).
4. **Prompt UX**: the toolbar/status line (repo, run, tokens in 5h window, cooldowns), tab completion (commands, run-id prefixes, repo names, step ids), history file location on Windows, multi-line input, how the constellation and demon lines render above the prompt.
5. **Dependency decision**: your recommendation on `prompt_toolkit` vs alternatives, with the smallest change that delivers history + completion on Windows.
6. **Testing strategy** using fakes only: how to test the loop, completion and the threaded run without a real terminal.
7. **Risks and open questions**, ordered, and a **step-by-step build order** in small commits, each with a verification step. Say where one real (quota-using) check is justified, and keep it to one tiny call.
8. What to leave for 6c/6d, so 6b does not creep.

Be concrete and opinionated; give a recommendation rather than a survey. If something in my constraints conflicts with a good design, say so and propose the least-bad option.
