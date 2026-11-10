# Firmamentum v2

A small personal Python CLI (`orch`) that drives two **official** coding CLIs as worker agents:

- **Claude Code** (`claude -p`) on a Claude Pro login
- **Antigravity CLI** (`agy -p`) on a Google AI Pro login

It never touches login tokens. It only spawns the two executables, passes a prompt, and reads their JSON. One command plans a task, builds it step by step in a git worktree, runs your tests itself, has a reviewer check the diff, repairs failures, and stops for you before anything sensitive. It never merges to main without you and never pushes.

## How it works

```
orch run "<request>"
   |
   v
Planner (claude opus) --> plan.json (steps, files, risk flags)        [read-only]
   |
   v   for each step, in dependency order, in ONE git worktree
Builder (claude sonnet) edits files --> post-checks (protected paths, deps, removed tests)
   |                                      |
   |                                      +--> stop: awaiting_approval
   v
orch runs YOUR test_command --fail--> Repair (claude sonnet), max 3 rounds --> retest
   | pass
   v
Reviewer (claude opus -> sonnet -> agy fallback) reads the diff [read-only]
   | approve                 | reject --> Repair
   v
commit "orch(sN): title"  -->  next step  -->  run done
   |
   v
you:  orch diff  ->  orch merge   (nothing is pushed)
```

Roles come from `config.yaml` as ordered lists of `agent:model`. If the first entry hits a usage limit, the router cools it down and tries the next one once; if nothing is left the run pauses and `orch resume` continues it later.

## Requirements

- Windows 11 (tested), Python 3.12, [uv](https://docs.astral.sh/uv/), git
- Claude Code CLI, logged in with a **Claude Pro** account
- Antigravity CLI (`agy`), logged in with a **Google AI Pro** account

## Setup on Windows

1. Install the CLIs and make sure both resolve from a fresh terminal: `claude --version` and `orch version` (it also prints the `agy` version). If not, add their install folders to your user `PATH` and reopen the terminal.
2. Log in once by hand: run `claude`, choose **Claude account with subscription (Pro/Max)**, not "Anthropic Console / API billing". Run `agy` and sign in with the Google account that has AI Pro.
3. **Check `~/.claude/settings.json`** (`%USERPROFILE%\.claude\settings.json`). If it has an `env` block with `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` or Ollama/proxy settings, `claude -p` will talk to that endpoint instead of your Pro plan. Remove those entries or keep them out of the profile you use here. (`orch` also strips `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` from the child process environment, but it cannot undo what the settings file sets.)
4. Make sure `ANTHROPIC_API_KEY` is not set in your shell (`$env:ANTHROPIC_API_KEY` should be empty; remove it from user environment variables if present). With it set, Claude Code bills the API key instead of the Pro plan.
5. In this repo: `uv sync`, then `uv run pytest -q` (tests use a fake adapter and never call a real CLI) and `uv run orch version`.

## config.yaml

```yaml
roles:                                  # ordered fallback lists of agent:model
  planner:  [claude:opus, claude:sonnet]
  scout:    [agy:default]               # agy:default = agy with no --model
  builder:  [claude:sonnet]             # builder and repair: claude only (agy has no tool allow-list)
  reviewer: [claude:opus, claude:sonnet, agy:default]
  repair:   [claude:sonnet]
cooldown_minutes: 60                    # used when a limit message has no parsable reset time
approval_rules: [merge_to_main, ...]    # the stops listed under "Safety model"
review: per_step                        # per_step | off
max_repair_rounds: 3
repos:                                  # the ONLY source of test commands
  orch:     { path: ".", test_command: "uv run pytest -q", retest_command: "uv run pytest -q --lf" }
  practice: { path: "../orch-practice", test_command: "uv run pytest -q", retest_command: "uv run pytest -q --lf" }
```

- `test_command` gates every commit. `retest_command` (optional) is a quick re-run tried first in repair rounds.
- Relative repo paths resolve against the folder holding `config.yaml`. `ORCH_CONFIG` overrides the config path.
- `--model role=model` on any command puts that model first for the role (e.g. `--model builder=opus`).

## Commands

Every command that takes a run accepts `--repo <name-or-path>` (default: current directory) and a unique run-id prefix.

| Command | What it does | Example |
|---|---|---|
| `ask` | One prompt to one CLI, usage logged | `orch ask --agent claude --model sonnet --prompt "Reply PONG"` |
| `usage` | Tokens and calls per agent/model (`--window 5h`, `--json`) | `orch usage --window 24h` |
| `cooldowns` | Show (or `--clear`) active rate-limit cooldowns | `orch cooldowns --clear claude` |
| `version` | orch and CLI versions (costs no quota) | `orch version` |
| `plan` | Ask the planner for a plan, store it | `orch plan "add a --json flag to usage" --repo orch` |
| `runs` / `show` | List runs / print a stored plan | `orch show 20261110T` |
| `exec` | Execute a stored plan in a new worktree | `orch exec 20261110T --repo orch` |
| `run` | Plan then execute in one go | `orch run "add password reset" --repo orch` |
| `resume` | Continue after a pause or crash (`--force` ignores resume_after) | `orch resume 20261110T` |
| `status` | Steps, tests, rounds, verdicts, tokens by role, what you need to do | `orch status 20261110T` |
| `approve` | Approve a stopped step and continue (`--retry` after repairs ran out) | `orch approve 20261110T --step s1` |
| `reject` | Reject a step and stop the run | `orch reject 20261110T --step s1` |
| `answer` | Answer a planner/builder question | `orch answer 20261110T --step s1 "use sqlite"` |
| `diff` | Show a run's diff (`--step sN`) | `orch diff 20261110T` |
| `merge` | `--no-ff` merge into your current branch, then run tests there | `orch merge 20261110T` |
| `clean` | Remove the worktree, keep the branch | `orch clean 20261110T` |

Exit codes of `exec/run/resume/approve`: 0 done, 1 failed, 2 needs input / approval / refused, 3 paused.

**Live view.** `exec`, `run`, `resume`, `approve` and `answer` show a live table (step, title, status, phase, role, entry used with `(fallback)` marked, repair round, tokens, elapsed) and a footer with total tokens and run status. When a run stops, a panel says what happened, why, and the exact next command. `--plain` (or any non-terminal stdout) gives plain line-by-line output with a `--- STOPPED ---` block instead.

**Practice mode.** `orch run/exec ... --hidden-tests DIR` copies the files of `DIR` into the worktree's `tests/` after the builder finishes the first step and before the checks, so the builder never saw them and a real repair round happens. For practice repos only: `DIR` must be outside the repo and any worktree, and file names must not already exist in `tests/`. Create the practice repo with `uv run python scripts/make_practice_repo.py` (makes `C:\projects\orch-practice` and the not-committed `C:\projects\orch-practice-hidden`).

## Safety model

- **Worktrees only.** Agents edit a git worktree under `%LOCALAPPDATA%\orch\wt\<repo>\<run>` on branch `orch/<run>`, outside your checkout. Your main checkout stays clean.
- **Path guards.** Destructive git calls (`reset --hard`, `clean`, `worktree remove`) refuse any path that is not a linked worktree under that root.
- **Read-only roles.** Planner and reviewer run claude with `--tools Read,Grep,Glob` and no prompts; scout/agy runs `--mode plan`. The builder and repair get Edit/Write and Bash limited to the configured test commands; git, `uv add`, `pip`, `rm`, `del` are denied.
- **Test command from config only.** The plan's `test_command` is never used. The orchestrator runs the tests itself and trusts the exit code, not an agent's claim.
- **Approval rules** (the run stops, you decide): touching auth/payment/secret paths or `.env`/keys; changing `pyproject.toml`/`uv.lock` (new dependency); removed tests or test files (`tests_removed`); tests still failing after 3 repair rounds; planner/builder questions; both agents rate limited; and anything the planner flags. Approval is per file and rule: a new flagged item stops again.
- **Nothing failing is committed**, and with review on, nothing is committed unreviewed.
- **Never pushes.** `merge` is local only and refuses on a dirty tree, a detached HEAD, or main/master without `--allow-main`.
- **Credentials.** `orch` never reads OAuth tokens or credential files; the child env for `claude` is scrubbed of `ANTHROPIC_*` variables.

## Costs

Each real call carries a large fixed system prompt, so even tiny prompts cost ~13k-24k tokens of plan quota. Measured in real runs (tokens include cache reads/writes):

| Call | Model | Tokens |
|---|---|---|
| `ask` "Reply PONG" | claude sonnet | 23,757 |
| `ask` "Reply PONG" | agy default | 13,268 |
| Planner (reads the repo) | claude opus | 78,001 - 87,485 |
| Builder, one step | claude sonnet | 84,458 - 120,257 |
| Reviewer, one diff | claude opus | 86,257 |
| Repair round | claude sonnet | see `docs/phase5b-notes.md` |

A one-step run with review is roughly 280k tokens; a two-step run about 450k. Use `orch usage` to see your own totals, and keep experiments tiny.

## Docs

`docs/PLAN.md` (plan and decisions), `docs/phase0..5b` notes (findings, design, real-run results and untested items).
