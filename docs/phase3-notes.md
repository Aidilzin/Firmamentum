# Phase 3 notes

## Step 0: CLI flags (`claude --help`, no quota used)
- **`--max-turns` does not exist** in this version's help. Not used. The turn cap is replaced by the 20 min builder timeout (process tree is killed on timeout).
- **`--max-budget-usd`** exists ("API calls", print mode only). Not used: meaningless on a subscription login and never tested.
- `--allowedTools` / `--disallowedTools`: comma or space separated, patterns like `Bash(git *)`. We pass one comma-joined argument each.
- `--permission-prompts none`: anything that would prompt is denied automatically.
- `--tools "<list>"` restricts the built-in toolset; `--append-system-prompt <text>` carries `roles/builder.md` (~1.5 KB, well under the Windows argv limit).

## Builder permissions (exact)
```
claude -p --output-format json --model sonnet --json-schema <build.json>
  --permission-prompts none
  --tools Read,Grep,Glob,Edit,Write,Bash
  --allowedTools Read,Grep,Glob,Edit,Write,Bash(<cfg test_command>),Bash(<cfg test_command> *)
  --disallowedTools Bash(git),Bash(git *),Bash(uv add*),Bash(uv pip*),Bash(pip *),Bash(rm *),Bash(del *)
  --append-system-prompt <builder.md>      cwd = worktree, timeout 20 min
```
- The test command is **always the repo's `test_command` from config.yaml**, for the allow rule and for the orchestrator's own test run. If the plan's `test_command` differs, a warning is logged (and stored in `meta.exec.warnings`) and config wins. A repo without a configured `test_command` is refused. A test uses `test_command = "git push"` in the plan and asserts it appears nowhere.
- `Bash(uv run python -c*)` was deliberately left out: it can write anywhere and use the network.
- agy cannot be the builder (no tool allow-list); `exec` refuses if the builder role is not `claude`.

## Design decisions
- **Worktree**: `%LOCALAPPDATA%\orch\wt\<repo>\<run_id>` (`~/.local/share/orch/wt/...` elsewhere), branch `orch/<run_id>` from the repo's HEAD. Outside the repo so pytest in the main checkout cannot collect it. `create` refuses if the repo has uncommitted changes (the gitignored-or-not `.orch/` run folder is excluded from that check).
- **Path guard** (`workspace._guard`): `reset --hard`, `clean`, `worktree remove --force` (and `commit_step`) resolve the path and raise `GuardError` unless it is under the worktree root, is not the root itself, is not the main repo, and has a `.git` *file* (a linked worktree). Tests aim every destructive call at the main repo, a bogus dir under the root, the root, its parent, and a real worktree elsewhere.
- **Step flow**: topological order, one at a time, one worktree. Before each step the worktree is reset to HEAD (guarded), so a crash or pause never leaves partial edits behind. After the builder: real changed files from `git status` (not the report's `files_changed`) -> warn on files outside the plan -> protected paths (`.env*`, `*.pem`, `*.key`, any `.git` component, `auth|payment|secret` anywhere in the path, case-insensitive) -> `awaiting_approval` (`touches_auth_payments_secrets_env`); `pyproject.toml` / `uv.lock` -> `awaiting_approval` (`new_dependency`). Then the orchestrator runs the test command (10 min timeout, shlex split, no shell), saves `steps/<id>/tests.txt`, pass/fail by exit code only. Pass: commit `orch(<id>): <title>` staging **only the files captured before the tests ran** (test artifacts are not committed), save `diff.patch`.
- **Stops**: risk-flagged step -> `awaiting_approval` before the builder is called. Builder blocked -> `needs_input` with the question. Test failure / builder error / timeout -> `failed`. `RATE_LIMITED` -> step back to `pending`, run `paused`.
- **Resume**: `pending` and crashed `running` steps continue. `failed`, `needs_input` and `awaiting_approval` steps are NOT auto-continued (Phase 4 adds `orch approve`); resume just reports them again.
- **meta.json** is written atomically (tmp + replace) after every transition. Per step: status, tokens, calls, session id, changed files, test result (exit code, duration), commit sha, warnings, question, error. Run level: `exec` (worktree, branch, base sha, warnings), `exec_tokens`.
- An invalid or missing builder report is only a warning: the tests and git are authoritative. A report with `blocked: true` is the only thing taken from it.
- `orch diff` runs git in the main repo against stored shas/branch, so it still works after `orch clean`.
- Exit codes of `exec`/`resume`/`run`: 0 done, 1 failed, 2 needs input / approval / precondition error, 3 paused.

## Real check
(not run yet; waiting for approval)

## Untested
- Whether a chained command such as `uv run pytest; git push` gets past the `Bash(uv run pytest*)`-style allow rule. The post-checks (changed files from git, protected paths, dependency files) and the worktree are the backstop; `Bash(git *)` is also on the deny list.
- Whether the real CLI honours `--tools` together with `--allowedTools` the way the help implies, and that denied tools really fail under `--permission-prompts none`.
- Whether the real `--json-schema` makes the builder return a valid `BuildReport` when it also uses tools for many turns.
- Real rate-limit text during a build (the existing patterns are guesses).
- `uv run pytest` inside a fresh worktree: it creates a `.venv` there (ignored in this repo). A target repo that does not gitignore its venv or caches could see them in `changed_files`; they would trigger out-of-plan warnings but not be committed unless they are among the files captured before the tests.
- Non-Windows worktree root and process-group kill.
