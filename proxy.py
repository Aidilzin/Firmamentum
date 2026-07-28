"""
ai-agent-factory -- interactive CLI orchestrator (v6)

Two ways to run it:
  python proxy.py                      -> interactive session (REPL)
  python proxy.py "task description"   -> one-shot mode, runs once and exits

Chat (/ask and auto-classified casual messages) runs on your local Ollama model
-- instant, free, no RunPod call. Coding tasks (/build and auto-classified tasks)
still go through the full PM -> Dev -> QA pipeline on RunPod. Set OLLAMA_MODEL in
.env to change which local model is used (default: qwen3:8b).

Visual layer uses `rich` (pip install rich) -- colored demon announcements, a
live spinner while waiting on RunPod instead of silent polling, syntax-highlighted
code previews, and real diffs during QA repair instead of full-file re-dumps.

In the REPL, plain text is auto-classified as either a quick chat message or a
coding task (see classify_intent). Override with /ask or /build if it guesses wrong.

Commands:
  /help              Show commands
  /status            Show target file, workspace contents, last result
  /history           List tasks completed this session
  /file <name>       Change the target file for future tasks
  /auto              Toggle auto-accept (skip write confirmation)
  /skills list       Show cached skills
  /skills search <k> Search trusted repos for a matching skill
  /skills add <repo> Trust a new repo (requires typed confirmation)
  /skills sync       Re-fetch all cached skills
  /ask <question>    Force chat mode (local model, no files touched)
  /build <task>      Force task mode (runs the PM->Dev->QA pipeline on RunPod)
  /clear             Clear session history
  /exit, /quit       Leave the session
"""

import os
import re
import sys
import difflib
import argparse
import subprocess
import httpx
import asyncio
from dotenv import load_dotenv

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Confirm, Prompt

import skill_manager

load_dotenv()

console = Console()

ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")
API_KEY = os.getenv("RUNPOD_API_KEY")

RUN_URL = f"https://api.runpod.ai/v2/{ENDPOINT_ID}/run"
STATUS_URL = f"https://api.runpod.ai/v2/{ENDPOINT_ID}/status"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

WORKSPACE_DIR = os.path.abspath("./workspace")
MODEL_NAME = "qwen/qwen3.6-27b-fp8"

LOCAL_MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen3:8b")
LOCAL_API_URL = "http://localhost:11434/v1/chat/completions"

MAX_POLL_WAIT_SECONDS = 600
POLL_INTERVAL_SECONDS = 3

TASK_KEYWORDS = {
    "build", "add", "fix", "implement", "create", "refactor", "debug",
    "update", "modify", "generate", "test", "optimize", "delete", "remove",
    "install", "configure", "bug", "error", "exception", "class", "function",
    "script", "endpoint", "api", ".py", ".js", ".ts"
}

# One color per demon, used consistently across every phase announcement.
DEMON_STYLE = {
    "laplace": ("cyan", "Laplace's Demon", "spec"),
    "maxwell": ("green", "Maxwell's Demon", "build"),
    "descartes": ("red", "Descartes' Demon", "repair"),
}


def announce_demon(key: str):
    color, name, role = DEMON_STYLE[key]
    console.print(f"\n[bold {color}]\u25c6 {name}[/bold {color}] [dim]-- {role}[/dim]")


def classify_intent(text: str) -> str:
    lowered = text.lower()
    return "task" if any(kw in lowered for kw in TASK_KEYWORDS) else "chat"


def read_local_codebase() -> str:
    code_bundle = []
    for root, _, files in os.walk(WORKSPACE_DIR):
        for file in files:
            if file.endswith(('.py', '.json')):
                full_path = os.path.join(root, file)
                with open(full_path, 'r') as f:
                    code_bundle.append(f"--- START FILE: {file} ---\n{f.read()}\n--- END FILE ---\n")
    return "".join(code_bundle)


def fetch_skill(sheet_name: str) -> str:
    with open(os.path.join("./daemon_skills", sheet_name), "r") as f:
        return f.read()


def extract_fenced_code(raw_response: str) -> str:
    pattern = r"```python\n(.*?)\n```"
    match = re.search(pattern, raw_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw_response.strip()


async def execute_cloud_inference(system_prompt: str, user_prompt: str, max_retries: int = 3) -> str:
    """Submits a job and polls for completion. Shows a live spinner instead of
    silent waiting, and gives up after MAX_POLL_WAIT_SECONDS with an explanation
    -- a long wait here almost always means a cold start is re-downloading the
    full model checkpoint."""
    payload = {
        "input": {
            "openai_route": "/v1/chat/completions",
            "openai_input": {
                "model": MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.1
            }
        }
    }

    last_error = None
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(max_retries):
            try:
                submit = await client.post(RUN_URL, headers=HEADERS, json=payload)
                submit.raise_for_status()
                job = submit.json()
                job_id = job["id"]
                status = job.get("status", "IN_QUEUE")

                elapsed = 0
                with console.status(f"[dim]Waiting on RunPod ({status})...[/dim]", spinner="dots") as live:
                    while status in ("IN_QUEUE", "IN_PROGRESS"):
                        if elapsed >= MAX_POLL_WAIT_SECONDS:
                            raise RuntimeError(
                                f"Job {job_id} still '{status}' after {MAX_POLL_WAIT_SECONDS}s -- giving up. "
                                "This almost always means a cold start is re-downloading the full model "
                                "checkpoint because no Network Volume is attached to the endpoint."
                            )
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                        elapsed += POLL_INTERVAL_SECONDS
                        live.update(f"[dim]Waiting on RunPod ({status}, {elapsed}s)...[/dim]")
                        poll = await client.get(f"{STATUS_URL}/{job_id}", headers=HEADERS)
                        poll.raise_for_status()
                        job = poll.json()
                        status = job.get("status")

                if status != "COMPLETED":
                    raise RuntimeError(f"RunPod job did not complete: {job}")

                output = job.get("output", {})
                if isinstance(output, list):
                    output = output[0] if output else {}
                return output["choices"][0]["message"]["content"]

            except (httpx.HTTPError, KeyError, RuntimeError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Cloud inference failed after {max_retries} attempts: {last_error}")


def execute_local_test_suite() -> tuple:
    execution = subprocess.run(
        [sys.executable, "-m", "pytest", "."],
        cwd=WORKSPACE_DIR,
        capture_output=True,
        text=True
    )
    return execution.returncode == 0, execution.stdout + execution.stderr


async def execute_local_inference(system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": LOCAL_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(LOCAL_API_URL, json=payload)
            if response.status_code >= 400:
                detail = response.text.strip()[:500]
                return (f"[Local model error {response.status_code}] {detail}\n"
                        f"Try 'ollama run {LOCAL_MODEL_NAME} \"hello\"' directly to see "
                        "the full error, and check whether it's a VRAM/resource issue.")
            data = response.json()
            return data["choices"][0]["message"]["content"]
    except httpx.ConnectError:
        return ("[Local model unreachable] Ollama doesn't seem to be running. "
                f"Start it, make sure '{LOCAL_MODEL_NAME}' is pulled (ollama pull {LOCAL_MODEL_NAME}), "
                "and try again.")
    except httpx.TimeoutException:
        return (f"[Local model timeout] '{LOCAL_MODEL_NAME}' took too long to respond -- "
                "possibly swapping out of VRAM into system RAM. Try again, or switch to a "
                "smaller model if this keeps happening.")
    except (httpx.HTTPError, KeyError) as e:
        return f"[Local model error] {e}"


def confirm_write(filename: str, code: str, previous_code: str = "") -> bool:
    """Show a syntax-highlighted preview (or a real diff, if there's a previous
    version) and ask before writing."""
    if previous_code:
        diff_lines = list(difflib.unified_diff(
            previous_code.splitlines(keepends=True),
            code.splitlines(keepends=True),
            fromfile=f"{filename} (before)",
            tofile=f"{filename} (after)"
        ))
        diff_text = "".join(diff_lines) if diff_lines else "(no textual changes)"
        console.print(Panel(
            Syntax(diff_text, "diff", theme="monokai", word_wrap=True),
            title=f"Changes to {filename}",
            border_style="yellow"
        ))
    else:
        console.print(Panel(
            Syntax(code, "python", theme="monokai", line_numbers=True, word_wrap=True),
            title=f"New file: {filename}",
            border_style="green"
        ))
    return Confirm.ask(f"Write this to workspace/{filename}?", default=False)


def gather_skill_context(user_instruction: str, auto_accept: bool) -> str:
    skill = skill_manager.find_relevant_skill(user_instruction)
    if skill:
        console.print(f"[dim]Using cached skill: {skill['name']}[/dim]")
        with open(skill["path"], "r") as f:
            return f"\n\nReference skill ({skill['name']}):\n{f.read()}\n"

    for keyword in skill_manager.extract_keywords(user_instruction):
        candidates = skill_manager.search_trusted_repos(keyword)
        if candidates:
            top = candidates[0]
            console.print(f"[dim]Found a matching skill in {top['repo']}: {top['name']}[/dim]")
            if skill_manager.fetch_and_cache_skill(top["repo"], top["path"], auto=auto_accept):
                skill_path = os.path.join(skill_manager.LIBRARY_DIR, top["name"], "SKILL.md")
                with open(skill_path, "r") as f:
                    return f"\n\nReference skill ({top['name']}):\n{f.read()}\n"
            break
    return ""


async def execute_agentic_pipeline(user_instruction: str, target_file: str, session_history: list, auto_accept: bool) -> dict:
    console.print("[dim]Scanning workspace architecture...[/dim]")
    current_repo_state = read_local_codebase()
    file_write_destination = os.path.join(WORKSPACE_DIR, target_file)

    previous_code = ""
    if os.path.exists(file_write_destination):
        with open(file_write_destination, "r") as f:
            previous_code = f.read()

    skill_context = gather_skill_context(user_instruction, auto_accept)

    history_context = ""
    if session_history:
        history_context = "Prior tasks completed this session:\n" + "\n".join(
            f"- {h['task']} (target: {h['file']}, result: {h['result']})" for h in session_history
        ) + "\n\n"

    announce_demon("laplace")
    pm_system = fetch_skill("laplace_spec.md")
    user_payload = f"{history_context}Task: {user_instruction}{skill_context}\n\nCurrent Repository Code:\n{current_repo_state}"
    specification = await execute_cloud_inference(pm_system, user_payload)
    console.print(Panel(Markdown(specification), title="Specification", border_style="cyan"))

    announce_demon("maxwell")
    dev_system = fetch_skill("maxwell_build.md")
    dev_payload = f"Follow this spec:\n{specification}{skill_context}\n\nExisting Code Context:\n{current_repo_state}"
    raw_ai_output = await execute_cloud_inference(dev_system, dev_payload)
    clean_source_code = extract_fenced_code(raw_ai_output)

    if not auto_accept and not confirm_write(target_file, clean_source_code, previous_code):
        console.print("[yellow]Write cancelled. Pipeline stopped for this task.[/yellow]")
        return {"task": user_instruction, "file": target_file, "result": "cancelled by user"}

    with open(file_write_destination, "w") as f:
        f.write(clean_source_code)
    console.print(f"[dim]Changes written to local workspace: {target_file}[/dim]")

    console.print("[dim]Running local test suite...[/dim]")
    tests_passed, runtime_logs = execute_local_test_suite()
    correction_iterations = 0

    while not tests_passed and correction_iterations < 3:
        correction_iterations += 1
        console.print(f"[bold red]Test failure detected[/bold red] [dim](loop {correction_iterations}/3)[/dim]")
        announce_demon("descartes")

        qa_system = fetch_skill("descartes_repair.md")
        qa_payload = f"Terminal Error Logs:\n{runtime_logs}{skill_context}\n\nBroken File Contents:\n{clean_source_code}"

        raw_ai_output = await execute_cloud_inference(qa_system, qa_payload)
        repaired_code = extract_fenced_code(raw_ai_output)

        if not auto_accept and not confirm_write(target_file, repaired_code, clean_source_code):
            console.print("[yellow]Write cancelled. Stopping QA loop.[/yellow]")
            return {"task": user_instruction, "file": target_file,
                    "result": f"cancelled during QA repair (loop {correction_iterations})"}

        clean_source_code = repaired_code
        with open(file_write_destination, "w") as f:
            f.write(clean_source_code)

        tests_passed, runtime_logs = execute_local_test_suite()

    if tests_passed:
        console.print("\n[bold green]\u2713 SUCCESS[/bold green] -- code passed all local verification tests.")
        return {"task": user_instruction, "file": target_file, "result": "success"}
    else:
        console.print("\n[bold red]\u2717 PIPELINE LIMIT[/bold red] -- failed to self-correct within 3 loops.")
        return {"task": user_instruction, "file": target_file, "result": f"failed after {correction_iterations} QA loops"}


async def handle_chat(message: str):
    console.print(f"[dim]({LOCAL_MODEL_NAME} via Ollama -- no files touched, no RunPod call)[/dim]")
    with console.status("[dim]Thinking...[/dim]", spinner="dots"):
        response = await execute_local_inference(
            "You are a helpful, concise assistant answering a developer's question. "
            "No code files are being changed right now; just answer directly.",
            message
        )
    console.print(Panel(Markdown(response), border_style="blue"))


HELP_TEXT = """\
[bold]/help[/bold]              Show this help message
[bold]/status[/bold]            Show target file, workspace contents, and last result
[bold]/history[/bold]           List tasks completed this session
[bold]/file[/bold] <name>       Change the target file for future tasks (current: {target})
[bold]/auto[/bold]              Toggle auto-accept (currently: {auto})
[bold]/skills list[/bold]       Show cached skills
[bold]/skills search[/bold] <k> Search trusted repos for a matching skill
[bold]/skills add[/bold] <repo> Trust a new repo (requires typed confirmation)
[bold]/skills sync[/bold]       Re-fetch all cached skills
[bold]/ask[/bold] <question>    Force chat mode (local model, no files touched)
[bold]/build[/bold] <task>      Force task mode (runs the PM->Dev->QA pipeline on RunPod)
[bold]/clear[/bold]             Clear session history
[bold]/exit[/bold], [bold]/quit[/bold]       Leave the session

Anything else is auto-classified as chat (local model) or a task (RunPod).
Override with /ask or /build if the guess is wrong.\
"""


def print_status(target_file: str, session_history: list):
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    files = sorted(os.listdir(WORKSPACE_DIR)) if os.path.isdir(WORKSPACE_DIR) else []
    table.add_row("Target file", target_file)
    table.add_row("Workspace files", ", ".join(files) if files else "(empty)")
    table.add_row("Last result", session_history[-1]['result'] if session_history else "(no tasks run yet)")
    console.print(table)


def print_history(session_history: list):
    if not session_history:
        console.print("[dim]No tasks run yet.[/dim]")
        return
    table = Table(title="Session History")
    table.add_column("#", justify="right")
    table.add_column("Result")
    table.add_column("Task")
    table.add_column("File")
    for i, h in enumerate(session_history, 1):
        result_style = "green" if h["result"] == "success" else "red" if "failed" in h["result"] else "yellow"
        table.add_row(str(i), f"[{result_style}]{h['result']}[/{result_style}]", h["task"], h["file"])
    console.print(table)


async def handle_skills_command(rest: str):
    if not rest:
        console.print("Usage: /skills list | /skills search <keyword> | /skills add <owner/repo> | /skills sync")
        return
    parts = rest.split(maxsplit=1)
    sub = parts[0]

    if sub == "list":
        skills = skill_manager.list_local_skills()
        if not skills:
            console.print("[dim]No skills cached yet.[/dim]")
        else:
            table = Table()
            table.add_column("Skill")
            table.add_column("Description")
            for s in skills:
                table.add_row(s["name"], s["description"])
            console.print(table)
    elif sub == "search" and len(parts) > 1:
        keyword = parts[1]
        matches = skill_manager.search_trusted_repos(keyword)
        if not matches:
            console.print(f"[dim]No matching skills found in trusted repos for '{keyword}'.[/dim]")
        else:
            for m in matches:
                fetch = Confirm.ask(f"Found '{m['name']}' in {m['repo']}. Download and cache it?", default=False)
                if fetch:
                    skill_manager.fetch_and_cache_skill(m["repo"], m["path"], auto=True)
    elif sub == "add" and len(parts) > 1:
        skill_manager.add_trusted_repo(parts[1].strip())
    elif sub == "sync":
        skill_manager.sync_all_cached_skills()
    else:
        console.print("Usage: /skills list | /skills search <keyword> | /skills add <owner/repo> | /skills sync")


async def run_repl():
    target_file = "app.py"
    auto_accept = False
    session_history = []

    console.print(Panel.fit(
        "[bold]ai-agent-factory[/bold]\n[dim]Type a task or question, or /help for commands.[/dim]",
        border_style="bold blue"
    ))

    while True:
        try:
            user_input = Prompt.ask("\n[bold blue]agent-factory[/bold blue]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Exiting.[/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            parts = user_input.split(maxsplit=1)
            command = parts[0]
            rest = parts[1] if len(parts) > 1 else ""

            if command in ("/exit", "/quit"):
                console.print("[dim]Exiting.[/dim]")
                break
            elif command == "/help":
                console.print(Panel(HELP_TEXT.format(target=target_file, auto=auto_accept), title="Commands"))
            elif command == "/status":
                print_status(target_file, session_history)
            elif command == "/history":
                print_history(session_history)
            elif command == "/file":
                if not rest:
                    console.print("Usage: /file <filename>")
                else:
                    target_file = rest.strip()
                    console.print(f"Target file set to: [bold]{target_file}[/bold]")
            elif command == "/auto":
                auto_accept = not auto_accept
                console.print(f"Auto-accept is now [bold]{'ON' if auto_accept else 'OFF'}[/bold]")
            elif command == "/clear":
                session_history = []
                console.print("[dim]Session history cleared.[/dim]")
            elif command == "/skills":
                await handle_skills_command(rest)
            elif command == "/ask":
                if rest:
                    await handle_chat(rest)
                else:
                    console.print("Usage: /ask <question>")
            elif command == "/build":
                if rest:
                    result = await execute_agentic_pipeline(rest, target_file, session_history, auto_accept)
                    session_history.append(result)
                else:
                    console.print("Usage: /build <task>")
            else:
                console.print(f"[red]Unknown command: {command}[/red]. Type /help for options.")
            continue

        intent = classify_intent(user_input)
        if intent == "chat":
            await handle_chat(user_input)
        else:
            result = await execute_agentic_pipeline(user_input, target_file, session_history, auto_accept)
            session_history.append(result)


async def run_once(task: str, target_file: str, auto_accept: bool):
    result = await execute_agentic_pipeline(task, target_file, [], auto_accept)
    sys.exit(0 if result["result"] == "success" else 1)


def main():
    parser = argparse.ArgumentParser(
        description="ai-agent-factory: local orchestrator for a cloud PM/Dev/QA pipeline"
    )
    parser.add_argument("task", nargs="?", help="Run once with this task, then exit. Omit for an interactive session.")
    parser.add_argument("-f", "--file", default="app.py", help="Target file for the task (default: app.py)")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-accept all writes and skill downloads")
    args = parser.parse_args()

    if args.task:
        asyncio.run(run_once(args.task, args.file, args.yes))
    else:
        asyncio.run(run_repl())


if __name__ == "__main__":
    main()