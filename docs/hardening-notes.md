# Hardening notes

11 Nov 2026 · branch `v2` · follows `docs/audit-fable.md` and `docs/probe-results.md`.
Everything here was built and tested with fakes only: no real `claude` or `agy` model call was made. The full suite
(`uv run pytest -q`) was green before each commit (320 tests at the end).

## What was done, one commit per item

| Item | Audit | Commit subject | What changed |
| --- | --- | --- | --- |
| Earlier batch | H4, H3, H5, C1 | Audit fixes: guard the worktree .git target ... | `_guard` checks where the worktree's `.git` really points and the branch; each step records its starting HEAD and stops with `git_state_changed` if it moves; `run_process` kills the child on Ctrl-C; the Bash allow rule is exact (`Bash(<cmd>)`, no `*`). |
| Probes | H1, H2, M6 | Worker flags: --restricted ... | `--restricted --strict-mcp-config` on every Claude worker call; `--mode plan --sandbox` for agy. Results in `docs/probe-results.md`. |
| 1 | M3, L3, L4 | Post-checks: config-driven ... | New `src/orch/checks.py` and `post_checks:` config block (protected, executes_later, dependency, migration, self_hosting). Matching uses `os.path.normcase`; `lstrip("./")` replaced by `removeprefix`. |
| 2 | M7 | Worker and test-run env from an allow-list ... | `child_env()` keeps only an allow-list plus the repo's `env_passthrough`; `ANTHROPIC_*` is dropped everywhere, even from `env_passthrough`. |
| 3 | M9 | Run orch's own git commit and merge without repo hooks ... | `commit_step` and `merge_no_ff` run with `-c core.hooksPath=<empty dir> -c core.fsmonitor=false`. |
| 4 | M10 | Detect rate limits only from stderr and structured error fields ... | Model text and stdout text no longer count. Claude: `api_error_status` 429, `subtype`, `terminal_reason`. agy: `status`. |
| 5 | M4 | Reviewer retry on a fallback entry ... | A retry that lands on a different entry gets the full prompt, the bad output and the feedback. |
| 6 | M6 (part) | agy reviewer is opt-in ... | `allow_agy_reviewer: false` by default; the router skips agy entries for the reviewer; config validation rejects an agy-only reviewer unless opted in. |
| 7 | docs | README: security model and limits ... | New "Security model and limits" section, updated safety table and config notes. |

## Decisions worth knowing

- **Protected paths changed meaning (item 1).** The old rule matched the substrings `auth`, `payment` and `secret` anywhere in
  a path. The new defaults follow your list: whole words `secret`, `credential`, `token`, `password` (so `my_secret.txt`,
  `auth-token.py` and `secrets.yaml` match; `secretary.md` and `tokenizer.py` do not) plus the file patterns. `auth` and
  `payment` are no longer protected. Add them to `post_checks.protected_words` if you want them back.
- **Word boundary.** A "word" ends at any character that is not a letter or digit, so `_`, `-`, `.` and `/` all count as
  boundaries. A trailing `s` is allowed (`secrets`).
- **Glob rules.** A pattern without `/` matches the file name at any depth (`conftest.py`, `.env*`); a pattern with `/` is
  anchored at the repo root (`.claude/**`, `src/orch/**`); `**/` crosses folders. All patterns are config data.
- **New rule names:** `executes_later` and `self_hosting`. Both are post-check stops with per-file approval like the existing
  ones. They are not in the planner's `RiskFlag` list, so the planner cannot set them and `approval_rules` is unchanged.
- **Self-hosting** is on only when the target repo resolves to the folder orch itself runs from.
- **Env allow-list (item 2).** Your list plus a few system variables that programs need to start on Windows and hold no
  secrets: `PATHEXT`, `COMSPEC`, `WINDIR`, `SYSTEMDRIVE`, `HOMEDRIVE`, `HOMEPATH`, `USERNAME`, `USERDOMAIN`, `PROGRAMDATA`,
  `PROGRAMFILES*`, `COMMONPROGRAMFILES`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`, `OS`, `LANG`, `LC_ALL`, `TERM`,
  `TZ`, `PYTHONUTF8`, `PYTHONIOENCODING`, `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_GIT_BASH_PATH`. Names are compared in upper
  case. Checked for free (no model call): `claude auth status --json` and `agy --version` still run under it.
- **Hooks (item 3).** The empty hooks folder is `<system temp>/orch-no-hooks`, created on demand. Both new tests fail
  without the fix (a failing pre-commit and pre-merge-commit hook aborts the git call).
- **Rate limits (item 4).** The CLIs' structured values use underscores (`error_rate_limit`), so those fields are matched
  both raw and with `_` and `-` turned into spaces. Cooldown scope and reset-time parsing still read the text of a call that
  was already classed as rate limited; only the detection changed.
- **agy reviewer (item 6).** `config.yaml` keeps `agy:default` in the reviewer list; it is simply skipped until you set
  `allow_agy_reviewer: true`. `orch doctor` wording for a missing agy was adjusted.

## Test changes you may want to look at

- `test_commands.py`: an arbitrary env var (`KEEP_ME`) used to be passed on; the test now asserts it is withheld.
- `test_executor.py`: the protected-path helper test was rewritten for the new defaults (`src/auth/login.py` and
  `src/Payment.py` are no longer protected). Dependency tests now cover `requirements-dev.txt`, `package.json`, `composer.lock`.
- `test_phase4.py`: the approval-per-file test uses `token_utils.py` instead of `auth_utils.py`.
- Four tests that exercise an agy reviewer now pass `allow_agy_reviewer=True`.

## Not verified

- No real run was made with the new env allow-list. A builder or reviewer call might need a variable that is not on it
  (symptom: a worker that cannot find its login or a tool). Fix by adding the name to the repo's `env_passthrough`.
- The agy reviewer runs `--mode plan --sandbox --add-dir <step folder>`; the combination was not probed.
- Item 1's executes-later list will stop steps that legitimately touch `conftest.py`, `CLAUDE.md` or CI files. That is the
  intent, but expect more approval stops on repos that change those often.

## Still open from the audit

- **C1:** the test process itself still runs agent-written code with your rights. Needs a real sandbox (own phase).
- **6b blockers:** H7 (run lock, `os.replace` retry), H6 (resume at the stored phase), D1 (cancel token), D3 (typed event
  stream), D4 (service layer).
- **Before 6c/6d:** D2 (streaming adapters), H8 + H9 (content-bound approvals on a tree id), D5, M12 (terminal escapes),
  M8 (run state location).
- **Medium/Low not touched:** M1, M2, M5, M11, M13, L1, L2, L5 to L12; the out-of-plan warning is still only a warning.
