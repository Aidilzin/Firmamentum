# Phase 6a notes: identity, theme, constellation view, `orch doctor`

Built and tested with fakes only (no real CLI call, no quota). `claude --help`, `agy --help` and one `claude auth status` (a local status command, no model call) were run to decide the login check.

## What was added
- `theme.py`: dark and light palettes (exact hex values from the plan), one demon registry (role key -> name, glyph, ASCII fallback, colour), `resolve_theme` (`--theme` > `ORCH_THEME` > config `ui.theme` > dark), `NO_COLOR` (no colour, glyphs stay), ASCII fallback with `--ascii` or when the stdout encoding cannot encode the glyphs. No background colour anywhere. Internal role keys and config keys are unchanged; `ui: {theme}` is a new optional config key.
- `view.py`: pure `RunView` built from `meta.json` (`build_run_view`): steps (id, title, status, current demon, phase, round, tokens, elapsed), current activity, totals, per-demon lines, `StopInfo` (what, why, next commands, border kind). No rich, no printing.
- `ui.py`: renderers over `RunView`: `render_rich` (constellation + demon lines + footer), `render_plain`, `render_stop_panel` (warning-colour border for "needs you" and paused, error colour for failures), `render_doctor`. `PlainView` and `LiveView` keep the executor interface (`log`, `on_update`). Executor log lines still say `running builder`; the views translate them (`demonize`) to `Maxwell (builder) building` (role in brackets on first mention only).
- `home.py`: the no-argument home screen (20 lines or fewer).
- `doctor.py`: the checks, all with injectable env, home, PATH lookup, subprocess and registry access.
- Executor: `started_at` / `ended_at` (UTC ISO) per step and per run in `meta.json`. Data only; no flow change.

## Decisions worth knowing
- **Step timestamps** are the latest running segment of a step (a step that is answered or retried starts a new segment); the run's `started_at` is its first start and `ended_at` its last stop. Waiting for a human is not counted inside a step, but is inside the run total.
- **Login type**: `claude auth status --json` exists and makes no model call, so doctor uses it and shows only the login method and plan. `agy` has no status command (`agy --help` lists none), so doctor prints `!` with how to check by hand.
- **agy missing** is `!`, and `✗` only when some role lists agy entries only. The shipped `config.yaml` has `scout: [agy:default]`, so by that rule a missing agy is `✗` there. Say so if scout should be exempt.
- `orch status` / `exec` output keeps its detailed table (tests pin it); role keys in it became demon names (`Maxwell=...`), and a `time` column was added.
- Elapsed time shown live comes from the live view's clock; stored runs use the new stamps, older metas show `-`.

## Tests
`tests/test_phase6a.py`: palettes, registry, theme choice, NO_COLOR, ASCII, RunView for done/running/needs approval/paused/failed, constellation strings, plain output, home (empty and populated DB), each doctor check with injected env/files (Ollama settings, API key set and masked, agy missing, repo without `test_command`), `--live` confirmation, timestamps.

## To look at by eye (needs a real terminal)
`uv run orch`, `uv run orch doctor`, `uv run orch status <a practice run>`; try `--theme light` and `--ascii`.
