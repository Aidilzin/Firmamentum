"""
skill_manager.py -- safe, curated skill discovery & caching for the agent factory.

Read this before editing TRUSTED_REPOS:

Skill content becomes part of the prompt sent to a code-writing model. A malicious
or compromised skill file is a prompt-injection / supply-chain risk, not just bad
advice. Because of that:

  - This module only ever auto-searches/auto-downloads from repos listed in
    TRUSTED_REPOS below.
  - Adding a new repo (via /skills add in the CLI) requires you to type the repo
    name back as confirmation -- a deliberate speed bump, not an oversight.
  - Once a skill is cached locally under daemon_skills/library/, it's treated as
    trusted local content from then on, same as pm_spec.md / dev_build.md.
"""

import os
import re
import json
import httpx
import yaml
from difflib import SequenceMatcher

LIBRARY_DIR = os.path.abspath("./daemon_skills/library")
REGISTRY_PATH = os.path.join(LIBRARY_DIR, "registry.json")

# Only these repos are ever auto-searched / auto-downloaded without asking first.
# Add more only after you've looked at the repo yourself -- use /skills add in the CLI.
TRUSTED_REPOS = [
    "anthropics/skills",  # Anthropic's official skill catalog. Most skills here are
                          # Apache-2.0; the docx/pdf/pptx/xlsx ones are source-available,
                          # not open source -- fine to reference, check license before redistributing.
]

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # optional: raises API rate limit from 60/hr to 5000/hr


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _load_registry() -> dict:
    if not os.path.exists(REGISTRY_PATH):
        return {}
    with open(REGISTRY_PATH, "r") as f:
        return json.load(f)


def _save_registry(registry: dict):
    os.makedirs(LIBRARY_DIR, exist_ok=True)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)


def _parse_frontmatter(skill_md_path: str) -> dict:
    with open(skill_md_path, "r") as f:
        content = f.read()
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if match:
        try:
            fm = yaml.safe_load(match.group(1)) or {}
            return {"name": fm.get("name", "unknown"), "description": fm.get("description", "")}
        except yaml.YAMLError:
            pass
    return {"name": os.path.basename(os.path.dirname(skill_md_path)), "description": ""}


def list_local_skills() -> list:
    """Metadata for every skill already cached under daemon_skills/library/."""
    skills = []
    if not os.path.isdir(LIBRARY_DIR):
        return skills
    for name in sorted(os.listdir(LIBRARY_DIR)):
        skill_md = os.path.join(LIBRARY_DIR, name, "SKILL.md")
        if os.path.isfile(skill_md):
            meta = _parse_frontmatter(skill_md)
            meta["path"] = skill_md
            skills.append(meta)
    return skills


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_relevant_skill(task_text: str, threshold: float = 0.28):
    """Score cached skills against the task text. Simple word-overlap + fuzzy
    string match -- no embeddings, no extra dependencies. Returns the best match
    above threshold, or None."""
    best, best_score = None, 0.0
    task_words = set(re.findall(r"\w+", task_text.lower()))
    for skill in list_local_skills():
        haystack = f"{skill['name']} {skill['description']}"
        skill_words = set(re.findall(r"\w+", haystack.lower()))
        overlap = len(task_words & skill_words) / max(len(skill_words), 1)
        fuzzy = _similarity(task_text, haystack)
        combined = max(overlap, fuzzy)
        if combined > best_score:
            best, best_score = skill, combined
    return best if best_score >= threshold else None


def extract_keywords(text: str, limit: int = 5) -> list:
    """Pull a handful of candidate domain keywords out of a task description."""
    stopwords = {"build", "create", "make", "with", "that", "have", "from", "this",
                 "using", "should", "would", "raise", "when", "there", "into"}
    words = re.findall(r"[a-zA-Z]{4,}", text.lower())
    seen, out = set(), []
    for w in words:
        if w not in stopwords and w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= limit:
            break
    return out


def search_trusted_repos(keyword: str) -> list:
    """Search only TRUSTED_REPOS for skill folders whose name matches the keyword."""
    results = []
    with httpx.Client(timeout=15.0) as client:
        for repo in TRUSTED_REPOS:
            try:
                resp = client.get(f"{GITHUB_API}/repos/{repo}/contents/skills", headers=_headers())
                if resp.status_code != 200:
                    continue
                for entry in resp.json():
                    if entry.get("type") == "dir" and keyword.lower() in entry["name"].lower():
                        results.append({"repo": repo, "path": entry["path"], "name": entry["name"]})
            except httpx.HTTPError:
                continue
    return results


def fetch_and_cache_skill(repo: str, skill_path: str, auto: bool = False) -> bool:
    """Download a skill's SKILL.md from a trusted repo and cache it locally."""
    if repo not in TRUSTED_REPOS:
        print(f"Blocked: {repo} is not in your trusted sources. Add it first with /skills add {repo}.")
        return False

    content, found = None, False
    with httpx.Client(timeout=15.0) as client:
        for branch in ("main", "master"):
            raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{skill_path}/SKILL.md"
            resp = client.get(raw_url)
            if resp.status_code == 200:
                content = resp.text
                found = True
                break

    if not found:
        print(f"Could not fetch SKILL.md for {skill_path} from {repo}.")
        return False

    skill_name = skill_path.rstrip("/").split("/")[-1]

    if not auto:
        print(f"\n--- Skill preview: {skill_name} (from {repo}) ---")
        preview = content[:500] + ("..." if len(content) > 500 else "")
        print(preview)
        print("--- End preview ---")
        confirm = input("Cache this skill locally for future tasks? [y/N] ").strip().lower()
        if confirm != "y":
            print("Skipped.")
            return False

    dest_dir = os.path.join(LIBRARY_DIR, skill_name)
    os.makedirs(dest_dir, exist_ok=True)
    with open(os.path.join(dest_dir, "SKILL.md"), "w") as f:
        f.write(content)

    registry = _load_registry()
    registry[skill_name] = {"source_repo": repo, "source_path": skill_path}
    _save_registry(registry)

    print(f"Cached skill '{skill_name}' locally.")
    return True


def sync_all_cached_skills():
    """Re-fetch every skill currently in the registry, in case the source updated."""
    registry = _load_registry()
    if not registry:
        print("No cached skills to sync.")
        return
    for skill_name, info in registry.items():
        print(f"Re-syncing {skill_name} from {info['source_repo']}...")
        fetch_and_cache_skill(info["source_repo"], info["source_path"], auto=True)


def add_trusted_repo(repo: str) -> bool:
    """Add a new repo to the trusted allowlist for THIS session only (in-memory).
    Edit TRUSTED_REPOS in this file directly to make it permanent across restarts."""
    if repo in TRUSTED_REPOS:
        print(f"{repo} is already trusted.")
        return False
    print(f"\nYou're about to trust '{repo}' as a source of skill files.")
    print("Skill content becomes part of the prompt sent to your coding model --")
    print("a malicious skill is a prompt-injection risk, not just bad advice.")
    print(f"Only proceed if you've looked at github.com/{repo} yourself.")
    confirm = input("Type the repo name again to confirm: ").strip()
    if confirm != repo:
        print("Confirmation did not match. Not added.")
        return False
    TRUSTED_REPOS.append(repo)
    print(f"Trusted for this session: {repo}")
    return True