# Phase 5a notes: routing with fallbacks and cooldowns

No real CLI was called in this phase. Everything is tested with fakes.

## Config
```yaml
roles:
  planner:  [claude:opus, claude:sonnet]
  scout:    [agy:default]
  builder:  [claude:sonnet]
  reviewer: [claude:opus, claude:sonnet, agy:default]
  repair:   [claude:sonnet]
cooldown_minutes: 60
```
- `cfg.roles[role]` is now a list of `Entry(agent, model)`; `entry.label` is `agent:model` (`agy:default` = no `--model`).
- The old `{agent, model}` form loads as a one-item list. `fallback_model` is accepted and ignored.
- builder and repair may only list claude entries (validated at load time; the executor keeps its old check as a guard). Empty lists are rejected.
- `--model role=model` puts that model first, on the agent of the role's first entry, and drops a duplicate.

## Router (`router.py`)
```
call_role(role, build_req, get_adapter, cfg)
  pick(role)              first entry not in cooldown; none -> paused, resume_after = earliest until_utc, NO call
  build_req(entry)        the caller builds the request per entry (agy reviewer: --mode plan + role text inline)
  run_agent(...)          logged in usage.db as before
  RATE_LIMITED            record cooldown, pick again, retry the same call once (max ONE hop)
                          second RATE_LIMITED, or nothing free -> paused, resume_after
```
- A third entry is not tried after two rate limits, even if free (explicit one-hop rule).
- Session ids (`resume_id`) are only used on the entry that created the session; any other entry gets a fresh prompt (planner retry, reviewer retry, repair).
- `fallback` in the records means "not the role's first entry".

## Cooldowns (`budget.py`, same SQLite file)
- Table `cooldowns(agent, model, until_utc, reason, created)`, primary key (agent, model). `model='*'` = every model of the agent. Writing the same key keeps the later `until_utc`.
- Scope: one model is cooled only if the message names exactly one model **and it is the model the failed call used**. A different model, several, or none cools `<agent>:*`. claude families: opus, sonnet, haiku (substring match, so `opus` and `claude-opus-*` agree). agy: single-model only for an explicit model whose name appears in the message; `agy:default` always cools `agy:*`.
- Until time (`router.parse_reset_time`), first believable match wins: ISO 8601 (naive = UTC), `|<epoch>`, `in/after N d/h/m/s` (several parts allowed), `resets at 5am` / `3:30 pm` / `17:00`, and the Claude Code `/usage` shapes `Resets 4:59am (Asia/Singapore)` and `Resets Nov 12, 7:59pm (Asia/Singapore)` (no "at", optional month-day, optional zone name in brackets). The bracketed zone is used when `zoneinfo` can resolve it; otherwise (unknown name, or no `tzdata` on Windows) the machine's own zone is used. Next occurrence if no date is given. A time in the past or more than 8 days away is rejected. Otherwise `now + cooldown_minutes`. The raw message is stored in `reason` (500 chars).
- Cooldowns are recorded by the router (it has the config), so `orch ask` does not record them.
- Expiry is by time only. `orch cooldowns --clear [agent]` removes them early.

## meta.json
- `resume_after` (UTC ISO) when a run pauses on a limit; removed by `Executor.resume`. For `orch plan/run` nothing can be resumed, so a planning pause is stored as a failed run with `resume_after` and the command exits 3.
- `calls_log`: `{step, role, entry, fallback}` per call. Step `roles_used[role]` = last `{entry, fallback}`. Planner and scout: `routing` and `models` (the entry that served the call).
- Step `session_entry` = entry that owns `session_id`.

## CLI
- `orch usage --window 5h|90m|24h|7d` (wins over `--days`); rows per agent and model. `--json` keeps `agents` and adds `window` and `models`.
- `orch cooldowns`; `orch cooldowns --clear [claude|agy]`.
- `orch resume`: paused with `resume_after` in the future -> local time printed, exit 3, unless `--force`. `--force` still respects cooldowns in the router (nothing is called while all entries cool); use `orch cooldowns --clear` when you know the limit has reset.
- `orch status`: `Paused until ...`, `Planning models: ...`, and per step `models used: reviewer=agy:default (fallback), ...`.

## Behaviour changes to know about
- A RATE_LIMITED pause now leaves a cooldown behind, so `orch resume` right away pauses again until the cooldown ends or is cleared. Three existing tests now clear cooldowns before resuming.
- A rate-limited reviewer on opus now moves to agy:default (after claude:* is cooled) instead of pausing at once.

## Untested live
- **Real rate-limit messages and reset-time formats.** `RATE_LIMIT_PATTERNS` and every parser shape above are guesses from common wording; no real message was captured. Check `reason` in `orch cooldowns` after the first real limit.
- Whether claude's message names the model for an Opus-only limit, and whether agy's quota errors name a model.
- **`tzdata` is not installed in this venv**, so named zones fall back to the machine zone here (same result while this laptop is in Asia/Singapore). The named-zone tests skip without it; they pass with `uv run --with tzdata pytest`. Adding `tzdata` as a dependency would make zone names exact; not done without your OK.
- Local-time handling of "resets at 5am" when the CLI's time zone differs from this machine's.
- agy as reviewer after a claude fallback (still the Phase 4 untested path).
- Whether a different model can resume another model's claude session (not attempted: it starts fresh).
