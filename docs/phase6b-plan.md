# orch Phase 6b Plan: Interactive Shell

Nov 11, 2026 · @Aidil

## Decisions at a glance

Typing `orch` in a terminal opens a shell that stays open, like `claude` or `agy`. Scripts and pipes keep the one-shot commands and the home screen.

- **Add `prompt_toolkit`** (one new dependency, plus `wcwidth`). Nothing else gives history, completion and safe printing above the prompt from a background thread on Windows.
- **Runs execute in one worker thread**, so the prompt stays usable during a run: `/status`, `/stop` and completion all keep working. Only one run is active per shell.
- **No rich `Live` inside the shell.** A live bottom toolbar shows the current demon and progress. Lasting events (step done, review verdict, stop panel) print above the prompt and stay in scrollback.
- **Reuse the Typer commands for quick slash commands** by calling the Click app in-process. Only the six long-running commands (`plan`, `run`, `exec`, `resume`, `approve`, `answer`) go through a new run controller.
- **One conflict with your constraints:** stopping a run safely needs a tiny executor hook (`should_stop`) plus a registry of live child processes. This is the least-bad option. Without it, Ctrl-C can only kill the whole shell.
- **Free text without a slash** means "run this task", but always behind a confirmation that defaults to No, because every run costs quota.

## Changes after the Fable audit

The audit (`docs/audit-fable.md`, 11 Nov) found six things that change this plan. Where this section and the sections below differ, this section wins.

| This plan said | Now | Audit |
| --- | --- | --- |
| Stop with a `should_stop` hook at save points, plus a process registry | A real cancel: `run_process(..., cancel=threading.Event)` polls and kills the tree; `Executor.cancel()` writes the step back as `pending` with its `resume_phase`; new outcome `cancelled`. One Ctrl-C or `/stop` sets it, no second press needed | D1, H5 |
| Event queue holding the live `meta` dict | Typed events (`StepStarted`, `PhaseChanged`, `AgentCalled`, `Stopped`), each with a frozen snapshot. `RunView` is built from snapshots on the main thread. `log()` stays for the plain view only | D3 |
| Quick commands through the Click app in-process | `services.py` returns data (`plan_run`, `drive`, `next_actions -> list[NextAction]`). Typer commands and slash commands are both thin printers over it | D4 |
| Toolbar and `/status` read `meta.json` | Never read `meta.json` of a run this shell is executing; use the latest snapshot. The executor holds a run lock (pid and heartbeat) and retries `os.replace` on `PermissionError`; dead runs show as `executing (stale)` | H7, D6 |
| Resume after a crash rebuilds the step | Resume continues at the stored phase; only `build` and `repair` reset the worktree, and a rebuild clears the per-attempt fields | H6 |
| Session state kept in the shell | One `Session` object (loaded config with reload, repo, theme, overrides) passed down, not module globals | D7 |

**Prep commits, before build step 1** (fakes only, full suite each time):

1. P1: run lock, `os.replace` retry, stale-run detection (H7, D6).
2. P2: resume at the stored phase, reset per-attempt fields on rebuild (H6).
3. P3: cancel token through `run_process`, router and executor, plus the `cancelled` outcome (D1).
4. P4: typed events with frozen snapshots (D3).
5. P5: `services.py` and `NextAction`; one-shot output unchanged (D4). **Done 12 Nov:** `plan_run`, `answer_plan`, `build_executor` + `drive`, `next_actions`, `diff_run`, `merge`, `clean_run`; the Typer commands only print. 76 golden files in `tests/golden/` pin the one-shot output. Also in P5: M11, and Ctrl-C during planning (exit 130).
6. P6: the `Session` object (D7).

After the prep, build step 5 below (services extraction) is already done, and build step 7 (stop) shrinks to wiring Ctrl-C and `/stop` to `Executor.cancel()`. Streaming (D2) and content-bound approvals (H8, H9, D5) stay in 6c and 6d.

## Architecture

The shell is a thin layer on top of what exists. Quick commands reuse the Typer app as it is. Only long runs get new code, a run controller with one worker thread.

&#91;embedded content: orch shell · 3 command paths, 1 worker thread\]

The worker thread never draws. It only fills a queue that the main thread turns into toolbar updates and lines above the prompt.

**Module layout**

```
src/orch/
  services.py        executor construction moved out of cli._drive (shared by one-shot and shell)
  shell/
    __init__.py      run_shell(session) entry point
    loop.py          PromptSession, key bindings, main loop, exit handling
    parse.py         parse_line -> Command | Task | Error
    registry.py      slash command table: name, path (session|click|run), args, completer kind, confirm text
    dispatch.py      session commands + in-process Click calls
    controller.py    RunController: thread, queue, stop, outcome
    console.py       ShellConsole: rich -> ANSI -> above the prompt
    toolbar.py       toolbar_text(...) pure function
    complete.py      OrchCompleter
    state.py         ShellSession + load/save shell.json
```

`cli.py` changes in only two places: the no-argument callback opens the shell when stdout is a terminal, and a new `orch shell` command is added.

**Session state** (`ShellSession`, one object owned by the main thread): repo (name and path), current run id, theme, ascii, model overrides (list of `role=model`), the active run handle or None, and a cached usage snapshot with its timestamp. Commands read defaults from it; only `/repo`, `/use`, `/model`, `/theme`, `/ascii` and the run controller change it.

## Long runs while the prompt stays open

Recommendation: run the blocking `Executor` call in one worker thread and keep the prompt on the main thread. A blocking design would make `orch` feel like a script again, which is the complaint this phase fixes.

**How it works**

1. `RunController.start(kind, args)` builds the executor through the extracted services (see Architecture) and calls `start()`, `resume()`, `approve()` or `answer()` in a `threading.Thread(daemon=False)`. Only one run at a time: starting a second one is refused with "a run is active, use /status or /stop".
2. The executor's `log` and `on_update` callbacks never print. They put events on a `queue.SimpleQueue` and store the latest `meta`.
3. The main thread does all the drawing:
   - The **bottom toolbar** rebuilds a `RunView` from the latest meta every 0.5 s (`refresh_interval`). This is the "live" part: constellation, current demon, elapsed time, tokens.
   - **Lasting events** are drained from the queue and printed above the prompt: step started, step done, review verdict, repair round, and the stop panel at the end. They are rendered with rich to an ANSI string and printed through `prompt_toolkit` inside `patch_stdout()`, so the input line is never corrupted.
4. When the thread ends, the controller prints the `ExecOutcome` and the next commands, then clears the active run. The finished run becomes the session's current run.

**Stopping a run (needs a small executor change)**

The executor has no cancel today. The least-bad option adds two small things:

- `Executor(..., should_stop=callable)`: checked only at the existing save points, before each agent call and before tests. If true, the run pauses with reason "stopped by you", using the same path as a rate-limit pause. `orch resume` then works as today.
- A registry of live child processes in `adapters/process.py` (data only), so a hard stop can kill the current agent's process tree with the existing `taskkill /T /F` helper.

| You press | Idle prompt | During a run |
| --- | --- | --- |
| Ctrl-C once | Clears the input line | Asks: "Stop after the current agent call? \[y/N\]". Yes sets `should_stop` |
| Ctrl-C again within 3 s | Nothing more | Kills the current agent process tree. The step is left `running`, and `/resume` resets the worktree to the last step commit before retrying |
| Esc | Clears the input line / closes the completion menu | Same (never stops a run) |
| Ctrl-D or `/exit` | Exits | Asks: wait for the run, stop and pause it, or cancel the exit |

Nothing is ever committed half-done: orch only commits after its own test run, and a killed step is reset by the existing crash-recovery path. Child agents already start with `CREATE_NEW_PROCESS_GROUP`, so pressing Ctrl-C in the shell does not reach them directly; this must be checked by hand once.

## Slash commands

Every slash command takes the same flags as its one-shot twin. `[run]` defaults to the session's current run and accepts a unique prefix or `@last`. `--repo` defaults to the session repo.

| Command | Syntax | Default and notes | Path |
| --- | --- | --- | --- |
| `/run` | `/run <task...> [--scout] [--model role=m]... [--hidden-tests DIR]` | Shows the demons and models it will use and "about 150k to 300k tokens", then asks \[y/N\] | Run controller |
| `/plan` | `/plan <task...> [--scout]` | Asks \[y/N\] (one Opus call) | Run controller |
| `/exec` | `/exec [run]` | Executes a stored plan | Run controller |
| `/resume` | `/resume [run] [--force]` |  | Run controller |
| `/approve` | `/approve [step] [--retry]` | Step defaults to the only step waiting for approval | Run controller |
| `/answer` | `/answer [step] <text...>` |  | Run controller |
| `/stop` | `/stop` | Same as the first Ctrl-C | Controller |
| `/reject` | `/reject [step]` | Asks \[y/N\] | Click |
| `/status` | `/status [run]` | Shows the full status; during a run, the live run | Click |
| `/runs` | `/runs` | Runs of the session repo | Click |
| `/show` | `/show [run]` |  | Click |
| `/diff` | `/diff [run] [--step sN]` |  | Click |
| `/merge` | `/merge [run] [--allow-main]` | Asks \[y/N\]; never pushes | Click |
| `/clean` | `/clean [run]` |  | Click |
| `/usage` | `/usage [--window 5h]` |  | Click |
| `/cooldowns` | `/cooldowns [--clear [agent]]` |  | Click |
| `/doctor` | `/doctor [--live]` | `--live` asks first | Click |
| `/ask` | `/ask --agent claude\|agy [--model m] <prompt...>` | Asks \[y/N\] | Click |
| `/use` | `/use <run>` | Sets the current run | Session |
| `/repo` | `/repo [name\|path]` | No argument lists repos | Session |
| `/model` | `/model [role=model ...] \| reset` | Session overrides for later runs; no argument shows them | Session |
| `/theme` | `/theme dark\|light` and `/ascii on\|off` | Redraws the toolbar | Session |
| `/clear` | `/clear` | Clears the screen only (not history, not the run) | Session |
| `/help` | `/help [command]` | Built from the registry and Click help | Session |
| `/exit` | `/exit` (alias `/quit`, Ctrl-D) | Asks first if a run is active | Session |

**Free text** (no leading `/`) is treated as a task:

```
✦ orch practice › make slugify keep digits
  Laplace (opus) plans, Maxwell (sonnet) builds, Loschmidt (opus) reviews.
  About 150k to 300k tokens. [r]un  [p]lan only  [N]o
```

The default is No. Up-arrow plus Enter on an old task asks again, so history can never start a run by accident. A line starting with `!` is refused with a hint, because orch is not a general shell.

## Prompt UX

The shell opens with the existing home screen printed once, then a one-line prompt and a one-line toolbar. Everything else scrolls above the prompt like normal terminal output.

```
✦ orch · Firmamentum v2          (home screen, printed once)
...
  ◆ Maxwell (builder) building s2 · Edit loop             (event lines, scrollback)
  ✓ s1 done · tests pass · Loschmidt approve · 874f6920
✦ orch practice › /status█
 ✦───✧───·  s2/3 ◆ Maxwell · 0:41 · run 112k · 5h 698k · Ctrl-C stop
```

- **Prompt:** `✦ orch <repo> ›` in the Laplace colour. During a run the star becomes the active demon's glyph and colour.
- **Toolbar, idle:** `repo practice · run 2026...1556 done · 5h 698k · cooldowns none · /help`. **During a run:** constellation, step x/y, active demon, elapsed, run tokens, 5h tokens, "Ctrl-C stop". Token totals come from the usage DB, cached for 30 s, so the toolbar never runs a slow query. A plain-text version is used with `--ascii` or `NO_COLOR`.
- **Completion** (menu with dim descriptions): slash commands and their flags; run-id prefixes for the session repo, newest first, with status as the description; repo names; step ids of the current run (only steps waiting for you, for `/approve` and `/answer`); `role=` keys and known models for `/model` and `--model`; paths for `--hidden-tests`.
- **Auto-suggest:** grey ghost text from history, accepted with the right arrow, like fish or Claude Code.
- **History:** `%LOCALAPPDATA%\orch\shell_history` (next to `usage.db`, never inside a repo), capped at 5,000 lines. Free-text tasks are stored as typed, so the confirmation still protects you on re-run.
- **Multi-line input:** a line ending in `\` continues on the next line, and pasted multi-line text arrives as one input (bracketed paste). Alt+Enter is not used, because Windows Terminal uses it for full screen.
- **Output above the prompt:** one `ShellConsole` renders rich renderables to ANSI with the current theme and prints them through `prompt_toolkit` under `patch_stdout(raw=True)`. Every command and the run controller print only through it, so stray `print` calls can't break the prompt.
- **Session memory:** the last repo and theme are saved to `%LOCALAPPDATA%\orch\shell.json` and restored on the next launch. Model overrides are not saved, so a session always starts from `config.yaml`.
- **When stdout is not a terminal:** `orch` with no arguments prints the home screen and exits, exactly as today. `orch shell` forces the shell, and `ORCH_NO_SHELL=1` turns it off.

## Dependency decision

Add `prompt_toolkit`. It is the only option that meets the thread requirement: printing run events while you type, without corrupting your input.

| Option | History | Completion | Toolbar | Safe print from a thread | Windows | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| stdlib `readline` | yes | yes | no | no | Not available | No |
| `pyreadline3` | yes | basic | no | no | Works, limited key handling and paste | No: run output would break the input line |
| rich only (`console.input`) | no | no | no | no | Works | No |
| Textual | yes | yes | yes | yes | Works | No: full screen, hides scrollback, out of scope |
| **`prompt_toolkit` 3.x** | yes | rich menus | yes | yes (`patch_stdout`) | Native Win32 and VT support | **Yes** |

**What it costs:** one pure-Python package plus `wcwidth`, about 1 MB installed, BSD licence. It is mature and widely used (IPython, ptpython, AWS CLI v2). It needs one adapter layer to print rich output (rich to ANSI, then `print_formatted_text`). Tests need its pipe input and dummy output instead of a real terminal. Pin `prompt_toolkit>=3.0,<4`.

**Smallest change that delivers history and completion:** `PromptSession(history=FileHistory(...), completer=OrchCompleter(...), auto_suggest=AutoSuggestFromHistory(), bottom_toolbar=..., refresh_interval=0.5)` inside `patch_stdout()`. Everything else in this plan builds on that one object.

## Testing with fakes only

Most of the shell is pure functions, so most tests need no terminal at all. Only one test file drives the real prompt loop, through `prompt_toolkit`'s pipe input.

| What | How | Key cases |
| --- | --- | --- |
| Slash parsing | Pure `parse_line(text) -> Command \| Task \| Error` | Quotes, flags, `@last`, `\` continuation, unknown command, `!` refused |
| Quick commands | In-process Click dispatch with a captured `ShellConsole` | Session repo and theme injected; `typer.Exit` codes caught; no `sys.exit` |
| Completion | Call `OrchCompleter.get_completions(Document(text), CompleteEvent())` | Commands, flags, run prefixes, steps waiting for you, `role=` keys |
| Toolbar | Pure `toolbar_text(session, run_view, usage, now)` | Idle, running, paused, needs you, `--ascii`, `NO_COLOR` |
| Run controller | Existing FakeAdapter and temp git repos; the thread joined with a timeout | Events in order, outcome, one-run-only refusal, current run set after |
| Stop | A fake adapter that blocks on a `threading.Event` | `should_stop` gives paused, and `/resume` finishes; a hard stop kills a `python -c sleep` child and leaves the step resumable |
| Loop | `create_pipe_input()` + `DummyOutput()`, feeding lines like `/repo practice\n/status\n/exit\n` | Dispatch calls; free text then `n` starts no run (asserts zero adapter calls); Ctrl-D exits |
| Not a terminal | Patch `isatty` | `orch` prints the home screen and exits 0 |
| Safety net | One autouse fixture makes the real adapters raise | No test can spend quota by accident |

All 300 existing tests must stay green after every commit. The one-shot CLI output does not change in 6b.

## Risks and open questions

The biggest risk is output from the run thread breaking the prompt. The second is the executor change needed for a safe stop.

| # | Risk | Mitigation |
| --- | --- | --- |
| 1 | Something prints directly (`typer.echo`, `print`, rich `console.print`) and corrupts the prompt | One `ShellConsole` for everything; `patch_stdout(raw=True)` catches stray prints; a test greps the shell modules for direct `print` |
| 2 | A safe stop needs `should_stop` and a process registry in the executor and `process.py` | Keep both as data and hook only, no flow change; tests for paused-then-resume and kill-then-resume |
| 3 | Spending quota by accident (free text, history, `/run` repeated) | Every call that costs quota asks \[y/N\] with the default No; the confirmation shows models and expected tokens |
| 4 | Windows keys: Alt+Enter is full screen in Windows Terminal; Ctrl-C reaching the child agents | Use `\` for multi-line; children run in a new process group; check both by hand once |
| 5 | SQLite used from two threads (worker writes usage, toolbar reads) | Check `budget.py` opens a connection per call; if not, `check_same_thread=False` plus a lock |
| 6 | Rich ANSI widths and glyphs differ between Windows Terminal and the old PowerShell 5.1 console | The existing `--ascii` fallback; test in both by hand |
| 7 | Reusing Click in-process: commands that call `sys.exit`, prompt, or read global options | `standalone_mode=False`, catch `typer.Exit` and `click.Abort`, pass theme and repo through `obj`/`default_map` |
| 8 | Exiting the shell during a run ends the run | `/exit` and Ctrl-D ask first; the stop path pauses cleanly so `orch resume` works later |

**Open questions** (each has a default; answer only if you disagree):

- [x] Approve the small executor change for `/stop` (`should_stop` hook and process registry)? Default: yes. Without it, 6b ships without `/stop`, and Ctrl-C during a run can only quit the whole shell.
- [x] Free text default: ask with \[r\]un / \[p\]lan only / \[N\]o. Alternative: free text always means plan only.
- [x] Should `orch` with no arguments always open the shell in a terminal (default), or only `orch shell`?

## Build order

Nine small commits on `v2`, each with the full suite green. Only commit 9 spends quota: one tiny call.

1. **Dependency and skeleton.** Add `prompt_toolkit` (after your OK). `orch shell` and no-argument `orch` in a terminal open a `PromptSession` with history, `/help` and `/exit`; not a terminal still prints the home screen. *Verify:* pipe-input loop test, non-tty test, then open it by hand.
2. **ShellConsole and idle toolbar.** Rich to ANSI above the prompt; theme and ASCII aware; idle toolbar from cached usage. *Verify:* toolbar snapshot tests, by hand in Windows Terminal.
3. **Parser, registry and quick commands.** `parse_line`, the session (`/repo`, `/use`, `/model`, `/theme`, `/ascii`, `/clear`), and in-process Click dispatch for `/status /runs /show /diff /usage /cooldowns /doctor /version /clean /reject /merge /ask`, with confirmations where listed. *Verify:* parser and dispatch tests; one-shot CLI tests unchanged.
4. **Completion and auto-suggest.** *Verify:* completer tests for every argument kind.
5. **Services extraction.** Move executor construction out of `cli._drive` into `services.py`. The one-shot commands call it. No behaviour change. *Verify:* all existing tests pass unchanged.
6. **Run controller.** Worker thread, event queue, live toolbar, event lines above the prompt, one-run limit, outcome printing; `/plan /run /exec /resume /approve /answer`. *Verify:* threaded fake-adapter tests.
7. **Stop.** `should_stop` hook, process registry, Ctrl-C rules, `/stop`, `/exit` during a run. *Verify:* blocking-fake and sleep-child tests; by hand: Ctrl-C during a fake run never reaches the child.
8. **Free text.** Task confirmation with \[r\]un / \[p\]lan only / \[N\]o. *Verify:* loop test proves `n` makes zero adapter calls.
9. **Docs and the one real check.** README "Shell" section and `docs/phase6b-notes.md`. Real check, with your OK: inside the shell, `/ask --agent claude --model haiku Reply PONG`. One tiny call proves the shell spawns a real CLI, prints above the prompt, and updates the toolbar's token count. Real `/run` threading is first tested on your next real task, not with extra quota now.

Each commit starts in plan mode, is approved with amendments, and is built with fakes, the same loop as Phases 0 to 6a.

## Left for 6c and 6d

6b is only the shell and its run control. Anything below is refused during 6b reviews, so the phase doesn't creep.

| Later phase | Item | What 6b does instead |
| --- | --- | --- |
| 6c | Live demon activity from `stream-json` ("Maxwell ▸ Edit src/x.py") | Phase-level toolbar only ("Maxwell building s2") |
| 6d | Inline approval keys `[a]pprove [r]eject [d]iff [q]uit` | The stop panel prints the commands; you type `/approve` |
| 6d | Syntax-highlighted diff viewer | `/diff` prints the existing diff |
| 6d | `orch init` setup wizard | Edit `config.yaml` by hand |
| 6d | Windows toast when a run stops | A terminal bell and the stop panel |
| Later | More than one run at a time | One active run per shell |
| Later | Full-screen app | `RunView` stays pure, so it can reuse the data |
| Later | Restoring model overrides and the current run between launches | Only the repo and theme are restored |
