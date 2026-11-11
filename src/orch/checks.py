"""Post-check path rules: which changed files stop a step for approval.

Patterns are globs on forward-slash paths. A pattern without `/` matches the file name at any depth; one with `/`
matches the whole path from the repo root (`**` crosses folders). Paths are compared with `os.path.normcase`, so a
case-insensitive file system cannot hide a file. Everything here is config data (`post_checks:` in config.yaml).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from .config import PostChecksConfig

RULE_PROTECTED = "touches_auth_payments_secrets_env"
RULE_EXECUTES_LATER = "executes_later"
RULE_DEPENDENCY = "new_dependency"
RULE_MIGRATION = "db_migration_or_schema"
RULE_SELF_HOSTING = "self_hosting"

ORCH_ROOT = Path(__file__).resolve().parents[2]


def norm_path(path: str) -> str:
    """Forward slashes, no leading `./` or `/`, normcase'd (lower case on Windows)."""
    p = os.path.normcase(path).replace("\\", "/")
    while p.startswith("./"):
        p = p.removeprefix("./")
    return p.strip("/")


@lru_cache(maxsize=512)
def _glob_re(pattern: str) -> re.Pattern[str]:
    pat = norm_path(pattern)
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile(out + r"\Z")


def matches(path: str, patterns: list[str]) -> bool:
    p = norm_path(path)
    name = p.rsplit("/", 1)[-1]
    for pat in patterns:
        rx = _glob_re(pat)
        if rx.match(name if "/" not in norm_path(pat) else p):
            return True
    return False


@lru_cache(maxsize=64)
def _word_re(words: tuple[str, ...]) -> re.Pattern[str] | None:
    if not words:
        return None
    alt = "|".join(re.escape(w.lower()) for w in words)
    # a "word" ends at any character that is not a letter or digit, so `my_secret.txt` and `auth-token.py` match
    # while `secretary.md` and `tokenizer.py` do not
    return re.compile(rf"(?<![a-z0-9])(?:{alt})s?(?![a-z0-9])")


def is_protected(path: str, cfg: PostChecksConfig) -> bool:
    if matches(path, cfg.protected):
        return True
    rx = _word_re(tuple(cfg.protected_words))
    return bool(rx and rx.search(norm_path(path)))


def is_self_hosting_repo(repo_path: Path) -> bool:
    try:
        return Path(repo_path).resolve() == ORCH_ROOT
    except OSError:
        return False


def classify(path: str, cfg: PostChecksConfig, self_hosting: bool = False) -> list[str]:
    """Every approval rule this changed file triggers (may be empty)."""
    rules: list[str] = []
    if is_protected(path, cfg):
        rules.append(RULE_PROTECTED)
    if matches(path, cfg.executes_later):
        rules.append(RULE_EXECUTES_LATER)
    if matches(path, cfg.dependency):
        rules.append(RULE_DEPENDENCY)
    if matches(path, cfg.migration):
        rules.append(RULE_MIGRATION)
    if self_hosting and matches(path, cfg.self_hosting):
        rules.append(RULE_SELF_HOSTING)
    return rules


def outside_plan(changed: list[str], planned: list[str]) -> list[str]:
    """Changed files the step's plan did not name. A planned entry ending in `/` covers its whole folder."""
    norm = [(norm_path(p), p.replace("\\", "/").endswith("/")) for p in planned]

    def planned_ok(f: str) -> bool:
        nf = norm_path(f)
        return any(nf == p or (is_dir and nf.startswith(p + "/")) for p, is_dir in norm)

    return [f for f in changed if not planned_ok(f)]
