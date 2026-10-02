#!/usr/bin/env python3
"""AI Bridge: deterministic queue, context isolation, and cost control for the multi-agent pipeline."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "agents.json"
STATE_DIR = ROOT / ".ai-bridge"
USAGE_PATH = STATE_DIR / "usage.json"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CIRCUIT_BREAKER = 2
EXIT_CI_NOT_GREEN = 3
EXIT_BUDGET_HALTED = 4
EXIT_PROTECTED_PATH = 5

RUNNING_STATES = ("claimed", "in_progress")
OPEN_PR_STATES = ("review", "changes_requested", "approved")
TERMINAL_STATES = ("merged", "failed", "escalated")

PLAN_MARKER = "<!-- ai-bridge:plan -->"
WORKER_MARKER = "<!-- ai-bridge:worker -->"
REVIEW_MARKER = "<!-- ai-bridge:review -->"
ESCALATION_MARKER = "<!-- ai-bridge:escalation -->"

CALLS_THIS_RUN = 0


class BridgeError(Exception):
    exit_code = EXIT_ERROR


class BudgetHalted(BridgeError):
    exit_code = EXIT_BUDGET_HALTED


class ProtectedPathError(BridgeError):
    exit_code = EXIT_PROTECTED_PATH


class CiNotGreen(BridgeError):
    exit_code = EXIT_CI_NOT_GREEN


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise BridgeError(f"missing config file: {CONFIG_PATH}")
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise BridgeError(f"invalid config JSON: {exc}") from exc
    governance = config.get("governance", {})
    if int(governance.get("max_concurrency", 0)) < 1:
        raise BridgeError("governance.max_concurrency must be >= 1")
    if int(governance.get("max_depth", 0)) < 1:
        raise BridgeError("governance.max_depth must be >= 1")
    if int(governance.get("review_retries", 0)) < 1:
        raise BridgeError("governance.review_retries must be >= 1")
    return config


def queue_path(config: dict[str, Any]) -> Path:
    return ROOT / config["governance"].get("queue_file", "tasks.json")


def read_queue(config: dict[str, Any]) -> dict[str, Any]:
    path = queue_path(config)
    if not path.exists():
        return {"version": 1, "tasks": []}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BridgeError(f"invalid queue JSON: {exc}") from exc
    data.setdefault("tasks", [])
    return data


class QueueTransaction:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.dirty = False

    def tasks(self) -> list[dict[str, Any]]:
        return self.data.setdefault("tasks", [])

    def find(self, task_id: str) -> dict[str, Any] | None:
        for task in self.tasks():
            if task.get("id") == task_id:
                return task
        return None

    def find_pr(self, pr_number: int) -> dict[str, Any] | None:
        for task in self.tasks():
            if task.get("pr") == pr_number:
                return task
        return None

    def touch(self, task: dict[str, Any], note: str = "") -> None:
        task["updated_at"] = iso(now())
        if note:
            history = task.setdefault("history", [])
            history.append({"at": task["updated_at"], "note": note})
            del history[:-20]
        self.dirty = True

    def save(self) -> None:
        self.dirty = True


@contextlib.contextmanager
def queue_txn(config: dict[str, Any]) -> Iterator[QueueTransaction]:
    path = queue_path(config)
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise BridgeError(f"invalid queue JSON: {exc}") from exc
        else:
            data = {"version": 1, "tasks": []}
        data.setdefault("tasks", [])
        transaction = QueueTransaction(data)
        yield transaction
        if transaction.dirty:
            data["updated_at"] = iso(now())
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, path)
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def run_cmd(cmd: list[str], input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise BridgeError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc


def gh(args: list[str], input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(["gh", *args], input_text=input_text, check=check)


def gh_json(args: list[str]) -> Any:
    proc = gh(args)
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise BridgeError(f"invalid JSON from gh: {exc}") from exc


def sanitize(config: dict[str, Any], text: str | None) -> str:
    if not text:
        return ""
    if not config.get("blind_collaboration", {}).get("enabled", True):
        return text
    result = text
    for term in config.get("blind_collaboration", {}).get("strip_identity_terms", []):
        result = re.sub(re.escape(term), "agent", result, flags=re.IGNORECASE)
    return result


def add_labels(config: dict[str, Any], kind: str, number: int, labels: list[str]) -> None:
    labels = [label for label in labels if label]
    if not labels:
        return

    def apply() -> subprocess.CompletedProcess:
        args = [kind, "edit", str(number)]
        for label in labels:
            args.extend(["--add-label", label])
        return gh(args, check=False)

    result = apply()
    if result.returncode != 0:
        for label in labels:
            gh(["label", "create", label, "--force"], check=False)
        apply()


def load_usage() -> dict[str, Any]:
    month = now().strftime("%Y-%m")
    if USAGE_PATH.exists():
        try:
            data = json.loads(USAGE_PATH.read_text())
            if data.get("month") == month:
                return data
        except json.JSONDecodeError:
            pass
    return {"month": month, "estimated_usd": 0.0, "calls": 0, "roles": {}}


def save_usage(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(data, indent=2) + "\n")


def check_budget(config: dict[str, Any]) -> None:
    usage = load_usage()
    budget = float(config["cost_controls"].get("monthly_budget_usd", 0))
    spent = float(usage.get("estimated_usd", 0.0))
    if budget <= 0:
        return
    percent = min(100.0, spent / budget * 100.0)
    warn_at = float(config["cost_controls"].get("warn_at_percent", 80))
    if percent >= warn_at:
        log(f"budget warning: {percent:.1f}% of ${budget:.2f} consumed (${spent:.4f})")
    if spent >= budget and config["cost_controls"].get("halt_on_budget_exceeded", True):
        raise BudgetHalted(f"monthly LLM budget exhausted: ${spent:.4f} / ${budget:.2f}")


def record_usage(config: dict[str, Any], role: str, usage: dict[str, Any]) -> None:
    data = load_usage()
    provider_name = config["roles"][role].get("provider", "economy")
    pricing = config.get("pricing", {}).get(provider_name, {})
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    cost = (
        input_tokens / 1000.0 * float(pricing.get("input_per_1k_usd", 0))
        + output_tokens / 1000.0 * float(pricing.get("output_per_1k_usd", 0))
    )
    entry = data.setdefault("roles", {}).setdefault(
        role, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0}
    )
    entry["calls"] += 1
    entry["input_tokens"] += input_tokens
    entry["output_tokens"] += output_tokens
    entry["estimated_usd"] = round(float(entry["estimated_usd"]) + cost, 6)
    data["calls"] = int(data.get("calls", 0)) + 1
    data["estimated_usd"] = round(float(data.get("estimated_usd", 0.0)) + cost, 6)
    save_usage(data)


def resolve_route(config: dict[str, Any], role: str) -> tuple[dict[str, Any], dict[str, Any], str, str, str]:
    roles = config.get("roles", {})
    if role not in roles:
        raise BridgeError(f"unknown role: {role}")
    route = roles[role]
    provider = config.get("providers", {}).get(route.get("provider"), {})
    base_url = (os.environ.get(provider.get("base_url_env", "")) or provider.get("base_url_default", "")).rstrip("/")
    api_key = os.environ.get(provider.get("api_key_env", ""), "")
    model = os.environ.get(route.get("model_env", "")) or route.get("model_default", "")
    return route, provider, base_url, api_key, model


def call_llm(
    config: dict[str, Any],
    role: str,
    system_prompt: str,
    user_prompt: str,
    dry_run: bool = False,
) -> str:
    global CALLS_THIS_RUN
    route, provider, base_url, api_key, model = resolve_route(config, role)
    if route.get("provider") == "premium" and role == "worker" and not config["cost_controls"].get(
        "allow_premium_in_worker", False
    ):
        raise BridgeError("worker role must use an economy provider (cost_controls.allow_premium_in_worker=false)")
    max_input = int(config["cost_controls"].get("max_input_chars", 48000))
    if len(system_prompt) + len(user_prompt) > max_input:
        user_prompt = user_prompt[: max(0, max_input - len(system_prompt))]
        log("input truncated to cost_controls.max_input_chars")
    if dry_run:
        log(f"[dry-run] role={role} provider={route.get('provider')} model={model or 'unset'}")
        log("[dry-run] system prompt:\n" + system_prompt)
        log("[dry-run] user prompt:\n" + user_prompt)
        return '{"dry_run": true}'
    max_calls = int(config["cost_controls"].get("max_llm_calls_per_run", 12))
    if CALLS_THIS_RUN >= max_calls:
        raise BridgeError(f"max_llm_calls_per_run reached ({max_calls})")
    if not base_url or not api_key or not model:
        raise BridgeError(
            f"role '{role}' endpoint not configured: set {provider.get('base_url_env')}, "
            f"{provider.get('api_key_env')}, {route.get('model_env')}"
        )
    check_budget(config)
    payload = {
        "model": model,
        "temperature": route.get("temperature", 0.2),
        "max_tokens": min(
            int(route.get("max_output_tokens", 4000)),
            int(config["cost_controls"].get("max_output_tokens_per_call", 8000)),
        ),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(route.get("timeout_seconds", 180))) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise BridgeError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BridgeError(f"LLM connection failed: {exc.reason}") from exc
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not text:
        raise BridgeError("LLM returned empty content")
    CALLS_THIS_RUN += 1
    record_usage(config, role, body.get("usage", {}))
    return text


def extract_json(text: str) -> Any:
    candidates = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL) + [text]
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for match in re.finditer(r"\{", candidate):
            start = match.start()
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(candidate)):
                char = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[start : index + 1])
                        except json.JSONDecodeError:
                            break
    raise BridgeError("model response did not contain valid JSON")


def normalize_path(value: str) -> str:
    return str(value).replace("\\", "/").lstrip("./")


def is_protected(config: dict[str, Any], path: str) -> bool:
    normalized = normalize_path(path)
    for pattern in config["context"].get("protected_paths", []):
        if fnmatch(normalized, pattern) or fnmatch(normalized, normalize_path(pattern)):
            return True
    return False


def safe_repo_path(
    config: dict[str, Any],
    path: str,
    task: dict[str, Any] | None = None,
    for_write: bool = False,
) -> Path:
    normalized = normalize_path(path)
    if not normalized or normalized.startswith("..") or normalized.startswith("/"):
        raise ProtectedPathError(f"illegal path: {path}")
    resolved = (ROOT / normalized).resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError as exc:
        raise ProtectedPathError(f"path escapes repository: {path}") from exc
    if is_protected(config, normalized):
        raise ProtectedPathError(f"protected path: {normalized}")
    if for_write and task is not None:
        files = [normalize_path(item) for item in task.get("files", [])]
        allowed = [str(item) for item in task.get("allowed_paths", [])]
        if normalized not in files and not any(fnmatch(normalized, pattern) for pattern in allowed):
            raise ProtectedPathError(f"path outside task scope: {normalized}")
    return resolved


def build_context(config: dict[str, Any], task: dict[str, Any]) -> str:
    max_files = int(config["context"].get("max_files_per_task", 8))
    files = list(dict.fromkeys(task.get("files", [])))
    if len(files) > max_files:
        raise BridgeError(f"task {task['id']}: {len(files)} files exceeds context.max_files_per_task={max_files}")
    budget = int(config["context"].get("max_context_tokens", 32000)) * int(
        config["context"].get("chars_per_token", 4)
    )
    chunks: list[str] = []
    used = 0
    for relative in files:
        absolute = ROOT / normalize_path(relative)
        if not absolute.exists():
            chunks.append(f"### FILE: {relative}\n(missing; create it if the task requires)\n")
            continue
        content = absolute.read_text(errors="replace")
        remaining = budget - used
        if remaining <= 0:
            raise BridgeError(f"task {task['id']}: context window exhausted before {relative}")
        if len(content) > remaining:
            content = content[:remaining] + "\n...(truncated)"
        used += len(content)
        chunks.append(f"### FILE: {relative}\n```\n{content}\n```\n")
    return "\n".join(chunks)


def changed_line_count(before: str, after: str) -> int:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
    return sum(1 for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---")))


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run_cmd(["git", *args], check=check)


def ensure_branch(branch: str, base: str, reuse: bool) -> None:
    if reuse:
        git(["fetch", "origin", branch])
        git(["checkout", "-B", branch, f"origin/{branch}"])
        return
    ref = f"origin/{base}"
    probe = git(["rev-parse", "--verify", ref], check=False)
    git(["checkout", "-B", branch, ref if probe.returncode == 0 else base])


def find_open_pr(branch: str) -> int | None:
    proc = gh(["pr", "list", "--head", branch, "--state", "open", "--limit", "1", "--json", "number"], check=False)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return int(data[0]["number"]) if data else None


def ensure_ci_green(config: dict[str, Any], pr_number: int) -> None:
    if not config["governance"].get("require_ci_pass", True):
        return
    proc = gh(["pr", "checks", str(pr_number), "--json", "name,state,bucket"], check=False)
    try:
        checks = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        checks = []
    if not checks:
        raise CiNotGreen(f"PR #{pr_number}: no CI checks found; deterministic CI must pass before review")
    failing = [check for check in checks if check.get("bucket") not in ("pass", "skipping")]
    if failing:
        names = ", ".join(check.get("name", "?") for check in failing)
        raise CiNotGreen(f"PR #{pr_number}: non-green CI checks: {names}")


PM_SYSTEM = """You are the Project Manager agent in an autonomous software pipeline.
You never write code. You decompose exactly one feature spec into small, independent, PR-sized tasks.
Hard rules:
- Return STRICT JSON only, no prose and no markdown outside the JSON.
- Produce at most __MAX_TASKS__ tasks.
- Each task must touch at most __MAX_FILES__ files and at most __MAX_DIFF__ changed lines.
- Name exact repository-relative file paths for every task.
- Use ONLY these routing labels: __LABELS__.
- A worker sees ONLY the listed files, so the spec must be self-contained.
- Never target protected paths: __PROTECTED__.
- depends_on must reference other task ids and form a DAG. Never create sub-agents.
- Every task depth is 1; workers cannot delegate further.
JSON schema:
{"tasks": [{"id": "T-001", "title": "short imperative title", "spec": "self-contained implementation instructions", "agent_label": "agent:backend", "files": ["src/example.py"], "allowed_paths": ["src/**"], "acceptance_criteria": ["observable testable condition"], "depends_on": [], "estimate": "S"}]}
"""

PM_USER = """Master Spec Issue #__ISSUE__: __TITLE__

SPEC BODY:
__BODY__

Existing queue tasks (do not duplicate work; reuse ids only if needed): 
__EXISTING__

Return the tasks JSON now."""

WORKER_SYSTEM = """You are a Worker agent. You implement exactly one queued task and nothing else.
Hard rules:
- Return STRICT JSON only, no prose and no markdown outside the JSON.
- Provide the FULL content of every file you create or modify.
- Write only to the listed target files or paths matching the allowed globs.
- Never touch protected paths: __PROTECTED__.
- Never modify CI, configuration, secrets, or the queue.
- You cannot spawn agents, open issues, or comment anywhere.
- Keep the change minimal; at most __MAX_DIFF__ changed lines.
- If a target file is marked missing, create it.
JSON schema:
{"summary": "what changed and why", "files": [{"path": "src/example.py", "content": "full file content"}], "notes": ""}
"""

WORKER_USER = """TASK __ID__: __TITLE__
ROUTING: __LABEL__
DEPTH: __DEPTH__ (maximum allowed: __MAX_DEPTH__; you cannot delegate)

SPEC:
__SPEC__

ACCEPTANCE CRITERIA:
__CRITERIA__

TARGET FILES (full current contents, context-isolated):
__CONTEXT__

REVIEW FEEDBACK TO ADDRESS (empty on first run):
__FEEDBACK__

Return the JSON now."""

REVIEWER_SYSTEM = """You are the Code Reviewer agent. Deterministic CI has already passed.
Judge only: correctness against acceptance criteria, scope creep, security, and maintainability.
Hard rules:
- Return STRICT JSON only.
- Approve when every acceptance criterion is met; request changes only for blocking defects.
- Never suggest touching protected paths (CI, config, bridge, queue, secrets).
JSON schema:
{"verdict": "approve", "summary": "one paragraph", "blocking_issues": [], "nitpicks": []}
Set verdict to "request_changes" to block."""

REVIEWER_USER = """TASK __ID__: __TITLE__
ACCEPTANCE CRITERIA:
__CRITERIA__

PULL REQUEST DIFF:
__DIFF__

REVIEW CYCLE: __CYCLE__ of __MAX_CYCLES__ allowed before escalation.
Return the JSON verdict now."""


def render_pm_system(config: dict[str, Any]) -> str:
    routing = config.get("labels", {}).get("routing", {})
    return (
        PM_SYSTEM.replace("__MAX_TASKS__", str(config["governance"].get("max_tasks_per_spec", 12)))
        .replace("__MAX_FILES__", str(config["context"].get("max_files_per_task", 8)))
        .replace("__MAX_DIFF__", str(config["cost_controls"].get("max_pr_diff_lines", 600)))
        .replace("__LABELS__", ", ".join(routing.keys()))
        .replace("__PROTECTED__", ", ".join(config["context"].get("protected_paths", [])))
    )


def render_worker_system(config: dict[str, Any]) -> str:
    return WORKER_SYSTEM.replace("__PROTECTED__", ", ".join(config["context"].get("protected_paths", []))).replace(
        "__MAX_DIFF__", str(config["cost_controls"].get("max_pr_diff_lines", 600))
    )


def assert_acyclic(tasks: list[dict[str, Any]]) -> None:
    graph = {task["id"]: list(task.get("depends_on", [])) for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise BridgeError(f"dependency cycle detected at {node}")
        visiting.add(node)
        for dependency in graph.get(node, []):
            if dependency not in graph:
                raise BridgeError(f"task {node} depends on unknown task {dependency}")
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def validate_planned_tasks(config: dict[str, Any], payload: Any, issue_number: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list) or not payload["tasks"]:
        raise BridgeError("PM response must be a JSON object with a non-empty 'tasks' array")
    proposed = payload["tasks"]
    max_tasks = int(config["governance"].get("max_tasks_per_spec", 12))
    max_files = int(config["context"].get("max_files_per_task", 8))
    routing = config.get("labels", {}).get("routing", {})
    if len(proposed) > max_tasks:
        raise BridgeError(f"PM proposed {len(proposed)} tasks; cap is {max_tasks}")
    raw_ids = [str(item.get("id") or f"T-{index + 1:03d}") for index, item in enumerate(proposed)]
    if len(set(raw_ids)) != len(raw_ids):
        raise BridgeError("PM proposed duplicate task ids")
    mapping = {raw: f"T-{index + 1:03d}" for index, raw in enumerate(raw_ids)}
    tasks: list[dict[str, Any]] = []
    for index, item in enumerate(proposed):
        if not isinstance(item, dict):
            raise BridgeError("each task must be a JSON object")
        label = str(item.get("agent_label", ""))
        if label not in routing:
            raise BridgeError(f"task {raw_ids[index]} uses unknown routing label: {label}")
        files = [str(path) for path in item.get("files", [])]
        if not files:
            raise BridgeError(f"task {raw_ids[index]} has no target files")
        if len(files) > max_files:
            raise BridgeError(f"task {raw_ids[index]} lists {len(files)} files; cap is {max_files}")
        allowed_paths = [str(path) for path in item.get("allowed_paths", [])] or files
        if not item.get("title") or not item.get("spec"):
            raise BridgeError(f"task {raw_ids[index]} requires title and spec")
        dependencies = [mapping.get(str(dep), str(dep)) for dep in item.get("depends_on", [])]
        task = {
            "id": mapping[raw_ids[index]],
            "title": str(item["title"]).strip()[:200],
            "status": "pending",
            "agent_label": label,
            "depth": 1,
            "spec": str(item["spec"]).strip(),
            "acceptance_criteria": [str(criterion) for criterion in item.get("acceptance_criteria", [])][:20],
            "files": [normalize_path(path) for path in files],
            "allowed_paths": [str(path) for path in allowed_paths],
            "depends_on": dependencies,
            "priority": index + 1,
            "estimate": str(item.get("estimate", ""))[:4],
            "attempts": 0,
            "review_cycles": 0,
            "pr": None,
            "branch": None,
            "lease_until": None,
            "last_review": None,
            "source_issue": issue_number,
            "created_at": iso(now()),
            "updated_at": iso(now()),
            "history": [{"at": iso(now()), "note": f"planned from master spec issue #{issue_number}"}],
        }
        for path in task["files"]:
            safe_repo_path(config, path, task=task, for_write=True)
        tasks.append(task)
    assert_acyclic(tasks)
    return tasks


def escalation_body(config: dict[str, Any], task: dict[str, Any], reason: str) -> str:
    criteria = "\n".join(f"- [ ] {item}" for item in task.get("acceptance_criteria", [])) or "- (none recorded)"
    files = "\n".join(f"- `{item}`" for item in task.get("files", [])) or "- (none recorded)"
    review = task.get("last_review") or {}
    blocking = "\n".join(f"- {item}" for item in review.get("blocking_issues", [])) or "- (none recorded)"
    history = "\n".join(f"- {entry.get('at')}: {entry.get('note')}" for entry in task.get("history", [])[-10:])
    return f"""{ESCALATION_MARKER}
## Pipeline halted: task escalated to the Architect

| Field | Value |
|---|---|
| Task | `{task['id']}` — {task['title']} |
| Reason | {reason} |
| Review cycles | {task.get('review_cycles', 0)} |
| Attempts | {task.get('attempts', 0)} |
| Source spec | #{task.get('source_issue', '?')} |
| PR | {('#' + str(task['pr'])) if task.get('pr') else '(none)'} |

### Task spec
{task.get('spec', '')}

### Acceptance criteria
{criteria}

### Target files
{files}

### Last reviewer feedback
{blocking}

### Recent history
{history}

### Required action
The pipeline will **not** retry this task automatically. The Architect must re-scope, split, or clarify it,
then update the Master Spec and re-apply the `master-spec` label (or open a new spec issue).
"""


def create_escalation_issue(config: dict[str, Any], task: dict[str, Any], reason: str) -> str:
    if not config.get("escalation", {}).get("create_issue", True):
        return ""
    labels = [config["labels"]["escalation"], config["labels"]["architect"]]
    args = ["issue", "create", "--title", f"[Escalation] {task['id']}: {task['title']}", "--body-file", "-"]
    for label in labels:
        args.extend(["--label", label])
    login = config.get("escalation", {}).get("architect_github_login")
    if login:
        args.extend(["--assignee", login])
    proc = gh(args, input_text=sanitize(config, escalation_body(config, task, reason)))
    return proc.stdout.strip()


def mark_pipeline_halted(config: dict[str, Any], config_queue: dict[str, Any], escalation_url: str) -> None:
    issue_number = config_queue.get("master_spec_issue")
    if not issue_number:
        return
    label = config["labels"]["pipeline_halted"]
    gh(["issue", "edit", str(issue_number), "--add-label", label], check=False)
    message = sanitize(
        config,
        f"{ESCALATION_MARKER}\nPipeline halted for this spec. Escalation: {escalation_url or '(issue created)'}",
    )
    gh(["issue", "comment", str(issue_number), "--body-file", "-"], input_text=message, check=False)


def trip_circuit_breaker(config: dict[str, Any], task: dict[str, Any], reason: str) -> int:
    pr_number = task.get("pr")
    if pr_number and config.get("escalation", {}).get("close_pr", True):
        body = sanitize(
            config,
            f"{REVIEW_MARKER}\nCircuit breaker tripped: {reason}. Closing this PR and escalating to the Architect.",
        )
        gh(["pr", "comment", str(pr_number), "--body-file", "-"], input_text=body, check=False)
        close_args = ["pr", "close", str(pr_number)]
        if config.get("escalation", {}).get("delete_branch", False):
            close_args.append("--delete-branch")
        gh(close_args, check=False)
    escalation_url = create_escalation_issue(config, task, reason)
    with queue_txn(config) as queue:
        live = queue.find(task["id"])
        if live:
            live["status"] = "escalated"
            live["lease_until"] = None
            queue.touch(live, f"circuit breaker: {reason}")
    mark_pipeline_halted(config, read_queue(config), escalation_url)
    log(f"circuit breaker tripped for {task['id']}: {reason}")
    return EXIT_CIRCUIT_BREAKER


def fail_task(config: dict[str, Any], task_id: str, reason: str) -> None:
    escalate = False
    with queue_txn(config) as queue:
        task = queue.find(task_id)
        if not task:
            return
        task["attempts"] = int(task.get("attempts", 0)) + 1
        task["lease_until"] = None
        max_attempts = int(config["governance"].get("max_task_attempts", 2))
        if task["attempts"] >= max_attempts:
            task["status"] = "failed"
            queue.touch(task, f"failed after {task['attempts']} attempt(s): {reason}")
            escalate = True
        else:
            task["status"] = "pending"
            task["last_review"] = None
            queue.touch(task, f"attempt {task['attempts']} failed: {reason}")
    if escalate:
        live = read_queue(config)
        task = next((item for item in live.get("tasks", []) if item.get("id") == task_id), None)
        if task:
            trip_circuit_breaker(config, task, f"technical failure after {task.get('attempts', 0)} attempt(s)")


def cmd_plan(args: argparse.Namespace) -> int:
    config = load_config()
    issue = gh_json(
        ["issue", "view", str(args.issue), "--json", "number,title,body,author,labels"]
    )
    labels = [label["name"] for label in issue.get("labels", [])]
    master_label = config["labels"]["master_spec"]
    if master_label not in labels:
        raise BridgeError(f"issue #{issue['number']} is missing the '{master_label}' label")
    author = (issue.get("author") or {}).get("login", "")
    if author.endswith("[bot]"):
        raise BridgeError("refusing to plan an issue authored by a bot (loop prevention)")
    with queue_txn(config) as queue:
        snapshot = json.loads(json.dumps(queue.data))
        existing_tasks = queue.tasks()
    already_planned = snapshot.get("master_spec_issue") == issue["number"] and any(
        task.get("source_issue") == issue["number"] for task in existing_tasks
    )
    if already_planned and not args.force:
        log(f"issue #{issue['number']} is already planned; use --force to plan again")
        return EXIT_OK
    existing_summary = "\n".join(
        f"- {task['id']} [{task.get('status')}] {task.get('title')}" for task in existing_tasks
    ) or "- (none)"
    user_prompt = (
        PM_USER.replace("__ISSUE__", str(issue["number"]))
        .replace("__TITLE__", str(issue.get("title", "")))
        .replace("__BODY__", str(issue.get("body", "")))
        .replace("__EXISTING__", existing_summary)
    )
    raw = call_llm(config, "pm", render_pm_system(config), user_prompt, dry_run=args.dry_run)
    if args.dry_run:
        return EXIT_OK
    payload = extract_json(raw)
    new_tasks = validate_planned_tasks(config, payload, issue["number"])
    with queue_txn(config) as queue:
        if not args.force and any(task.get("source_issue") == issue["number"] for task in queue.tasks()):
            log("plan already applied by a concurrent run; skipping duplicate")
            return EXIT_OK
        queue.data["master_spec_issue"] = issue["number"]
        queue.tasks().extend(new_tasks)
        queue.save()
    rows = "\n".join(
        f"| `{task['id']}` | {task['title']} | `{task['agent_label']}` | {len(task['files'])} | "
        f"{', '.join(task['depends_on']) or '—'} |"
        for task in new_tasks
    )
    comment = sanitize(
        config,
        f"""{PLAN_MARKER}
## PM plan generated

Master spec: #{issue['number']} — {issue.get('title', '')}
Tasks queued: **{len(new_tasks)}** · Concurrency cap: {config['governance']['max_concurrency']} ·
Review retries: {config['governance']['review_retries']} · Auto-merge: {config['governance'].get('auto_merge', False)}

| ID | Task | Route | Files | Depends on |
|---|---|---|---|---|
{rows}

The queue engine will dispatch workers automatically. No stakeholder action is required.
""",
    )
    gh(["issue", "comment", str(issue["number"]), "--body-file", "-"], input_text=comment)
    add_labels(config, "issue", issue["number"], [config["labels"]["planned"]])
    log(f"planned {len(new_tasks)} tasks from issue #{issue['number']}")
    return EXIT_OK


def cmd_claim(args: argparse.Namespace) -> int:
    config = load_config()
    cap = int(config["governance"]["max_concurrency"])
    max_open = int(config["governance"].get("max_open_prs", cap))
    limit = max(1, min(int(args.limit), cap))
    lease_minutes = int(config["governance"].get("lease_minutes", 45))
    review_retries = int(config["governance"]["review_retries"])
    claimed: list[dict[str, str]] = []
    with queue_txn(config) as queue:
        tasks = queue.tasks()
        running = sum(1 for task in tasks if task.get("status") in RUNNING_STATES)
        open_prs = sum(1 for task in tasks if task.get("status") in OPEN_PR_STATES)
        slots = min(cap - running, max_open - open_prs, limit)
        if slots > 0:
            merged = {task["id"] for task in tasks if task.get("status") == "merged"}
            ordered = sorted(tasks, key=lambda task: (int(task.get("priority", 999)), task.get("id", "")))
            for task in ordered:
                if len(claimed) >= slots:
                    break
                status = task.get("status")
                if status == "changes_requested":
                    if int(task.get("review_cycles", 0)) >= review_retries:
                        continue
                elif status == "pending":
                    if not all(dep in merged for dep in task.get("depends_on", [])):
                        continue
                else:
                    continue
                task["status"] = "claimed"
                task["lease_until"] = iso(now() + timedelta(minutes=lease_minutes))
                queue.touch(task, "claimed by queue engine")
                claimed.append({"task_id": task["id"], "agent_label": task.get("agent_label", "")})
    matrix = json.dumps({"include": claimed}, separators=(",", ":"))
    log(f"claimed {len(claimed)} task(s): {[item['task_id'] for item in claimed]}")
    if args.github_output:
        with open(args.github_output, "a") as handle:
            handle.write(f"matrix={matrix}\n")
            handle.write(f"count={len(claimed)}\n")
    print(matrix)
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config()
    lease_minutes = int(config["governance"].get("lease_minutes", 45))
    with queue_txn(config) as queue:
        task = queue.find(args.task_id)
        if not task:
            raise BridgeError(f"unknown task: {args.task_id}")
        if task.get("status") not in ("claimed", "changes_requested", "pending"):
            raise BridgeError(f"task {args.task_id} is '{task.get('status')}'; expected claimed or changes_requested")
        if int(task.get("depth", 1)) > int(config["governance"]["max_depth"]):
            raise BridgeError(f"task {args.task_id} depth exceeds governance.max_depth")
        previous_status = task.get("status", "pending")
        task["status"] = "in_progress"
        task["lease_until"] = iso(now() + timedelta(minutes=lease_minutes))
        queue.touch(task, "worker started")
        task_snapshot = json.loads(json.dumps(task))
        task_snapshot["_previous_status"] = previous_status
    try:
        context = build_context(config, task_snapshot)
        feedback = task_snapshot.get("last_review") or {}
        feedback_text = ""
        if feedback:
            issues = "\n".join(f"- {item}" for item in feedback.get("blocking_issues", []))
            feedback_text = f"{feedback.get('summary', '')}\n{issues}".strip()
        criteria = "\n".join(f"- {item}" for item in task_snapshot.get("acceptance_criteria", []))
        user_prompt = (
            WORKER_USER.replace("__ID__", task_snapshot["id"])
            .replace("__TITLE__", task_snapshot["title"])
            .replace("__LABEL__", task_snapshot.get("agent_label", ""))
            .replace("__DEPTH__", str(task_snapshot.get("depth", 1)))
            .replace("__MAX_DEPTH__", str(config["governance"]["max_depth"]))
            .replace("__SPEC__", task_snapshot.get("spec", ""))
            .replace("__CRITERIA__", criteria)
            .replace("__CONTEXT__", context)
            .replace("__FEEDBACK__", feedback_text or "(none)")
        )
        raw = call_llm(config, "worker", render_worker_system(config), user_prompt, dry_run=args.dry_run)
        if args.dry_run:
            with queue_txn(config) as queue:
                live = queue.find(args.task_id)
                if live:
                    live["status"] = task_snapshot.get("_previous_status", "pending")
                    live["lease_until"] = None
                    queue.touch(live, "dry-run: status restored")
            return EXIT_OK
        payload = extract_json(raw)
        worker_files = payload.get("files")
        if not isinstance(worker_files, list) or not worker_files:
            raise BridgeError("worker response contains no files")
        max_files = int(config["context"].get("max_files_per_task", 8)) + 4
        if len(worker_files) > max_files:
            raise BridgeError(f"worker produced {len(worker_files)} files; cap is {max_files}")
        writes: list[tuple[Path, str]] = []
        for item in worker_files:
            if not isinstance(item, dict) or not item.get("path") or not isinstance(item.get("content"), str):
                raise BridgeError("each worker file needs 'path' and string 'content'")
            absolute = safe_repo_path(config, item["path"], task=task_snapshot, for_write=True)
            writes.append((absolute, item["content"]))
        max_diff = int(config["cost_controls"].get("max_pr_diff_lines", 600))
        total_changed = 0
        for absolute, content in writes:
            before = absolute.read_text(errors="replace") if absolute.exists() else ""
            total_changed += changed_line_count(before, content)
        if total_changed > max_diff:
            raise BridgeError(f"worker diff is {total_changed} lines; cap is {max_diff}")
        if total_changed == 0:
            raise BridgeError("worker produced no changes")
        for absolute, content in writes:
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(content)
        branch = task_snapshot.get("branch") or config["branching"]["branch_prefix"] + task_snapshot["id"].lower()
        base = config["branching"].get("base_branch", "main")
        reuse = bool(task_snapshot.get("branch"))
        ensure_branch(branch, base, reuse)
        git(["add", "--", *[normalize_path(item["path"]) for item in worker_files]])
        git(["commit", "-m", f"{config['branching'].get('commit_prefix', 'feat(agent)')}: {task_snapshot['id']} {task_snapshot['title']}"])
        git(["push", "origin", f"HEAD:refs/heads/{branch}"])
        pr_number = task_snapshot.get("pr") or find_open_pr(branch)
        if not pr_number:
            labels = [config["labels"]["auto_pr"], task_snapshot.get("agent_label", "")]
            criteria_boxes = "\n".join(f"- [ ] {item}" for item in task_snapshot.get("acceptance_criteria", []))
            body = sanitize(
                config,
                f"""{WORKER_MARKER}
Task `{task_snapshot['id']}` — {task_snapshot['title']}

{payload.get('summary', '')}

### Acceptance criteria
{criteria_boxes or '- [ ] (none recorded)'}

Generated by the autonomous pipeline. Deterministic CI must pass before agent review.
""",
            )
            create_args = [
                "pr",
                "create",
                "--title",
                f"{config['branching'].get('commit_prefix', 'feat(agent)')}: {task_snapshot['id']} {task_snapshot['title']}",
                "--body-file",
                "-",
                "--base",
                base,
                "--head",
                branch,
            ]
            for label in labels:
                if label:
                    create_args.extend(["--label", label])
            proc = gh(create_args, input_text=body)
            match = re.search(r"/pull/(\d+)", proc.stdout)
            if not match:
                raise BridgeError("could not parse PR number from gh output")
            pr_number = int(match.group(1))
        with queue_txn(config) as queue:
            live = queue.find(task_snapshot["id"])
            if live:
                live["status"] = "review"
                live["branch"] = branch
                live["pr"] = pr_number
                live["last_review"] = None
                live["lease_until"] = None
                queue.touch(live, f"PR #{pr_number} opened for review")
        log(f"task {task_snapshot['id']} produced PR #{pr_number}")
        return EXIT_OK
    except BridgeError as exc:
        log(f"task {args.task_id} failed: {exc}")
        fail_task(config, args.task_id, str(exc))
        raise


def cmd_review(args: argparse.Namespace) -> int:
    config = load_config()
    if not args.dry_run:
        ensure_ci_green(config, args.pr)
    with queue_txn(config) as queue:
        task = queue.find_pr(args.pr)
        if not task:
            raise BridgeError(f"PR #{args.pr} is not linked to a managed task")
        task_snapshot = json.loads(json.dumps(task))
    retries = int(config["governance"]["review_retries"])
    if int(task_snapshot.get("review_cycles", 0)) >= retries:
        return trip_circuit_breaker(config, task_snapshot, f"review retries exhausted ({retries})")
    diff = gh(["pr", "diff", str(args.pr)]).stdout
    max_input = int(config["cost_controls"].get("max_input_chars", 48000))
    if len(diff) > max_input // 2:
        diff = diff[: max_input // 2] + "\n...(diff truncated for cost control)"
    criteria = "\n".join(f"- {item}" for item in task_snapshot.get("acceptance_criteria", []))
    user_prompt = (
        REVIEWER_USER.replace("__ID__", task_snapshot["id"])
        .replace("__TITLE__", task_snapshot["title"])
        .replace("__CRITERIA__", criteria)
        .replace("__DIFF__", diff)
        .replace("__CYCLE__", str(int(task_snapshot.get("review_cycles", 0)) + 1))
        .replace("__MAX_CYCLES__", str(retries))
    )
    raw = call_llm(config, "reviewer", REVIEWER_SYSTEM, user_prompt, dry_run=args.dry_run)
    if args.dry_run:
        return EXIT_OK
    payload = extract_json(raw)
    verdict = str(payload.get("verdict", "request_changes")).lower()
    summary = sanitize(config, str(payload.get("summary", "")))
    blocking = [sanitize(config, str(item)) for item in payload.get("blocking_issues", [])]
    nitpicks = [sanitize(config, str(item)) for item in payload.get("nitpicks", [])]
    if verdict == "approve":
        approve_body = sanitize(
            config,
            f"{REVIEW_MARKER}\n## Agent review: approved\n\n{summary}\n\n"
            + ("Nitpicks:\n" + "\n".join(f"- {item}" for item in nitpicks) if nitpicks else ""),
        )
        gh(["pr", "review", str(args.pr), "--approve", "--body-file", "-"], input_text=approve_body)
        merged = False
        if config["governance"].get("auto_merge", True):
            proc = gh(["pr", "merge", str(args.pr), "--squash", "--delete-branch"], check=False)
            merged = proc.returncode == 0
        with queue_txn(config) as queue:
            live = queue.find(task_snapshot["id"])
            if live:
                live["status"] = "merged" if merged else "approved"
                live["lease_until"] = None
                queue.touch(live, "review approved" + (" and merged" if merged else "; awaiting manual merge"))
        if not merged and config["governance"].get("auto_merge", True):
            note = sanitize(config, f"{REVIEW_MARKER}\nAuto-merge failed (branch protection?); a human must merge.")
            gh(["pr", "comment", str(args.pr), "--body-file", "-"], input_text=note, check=False)
        log(f"PR #{args.pr} approved")
        return EXIT_OK
    cycles = int(task_snapshot.get("review_cycles", 0)) + 1
    last_review = {
        "at": iso(now()),
        "summary": summary,
        "blocking_issues": blocking or ["See review comment"],
    }
    with queue_txn(config) as queue:
        live = queue.find(task_snapshot["id"])
        if live:
            live["review_cycles"] = cycles
            live["status"] = "changes_requested"
            live["last_review"] = last_review
            live["lease_until"] = None
            queue.touch(live, f"review requested changes (cycle {cycles}/{retries})")
    add_labels(config, "pr", args.pr, [f"{config['labels'].get('strike_prefix', 'strike:')}{cycles}"])
    if cycles >= retries:
        return trip_circuit_breaker(config, task_snapshot | {"review_cycles": cycles, "last_review": last_review}, f"{cycles} review cycles without approval")
    body = sanitize(
        config,
        f"""{REVIEW_MARKER}
## Agent review: changes requested ({cycles}/{retries})

{summary}

### Blocking issues
""" + ("\n".join(f"- {item}" for item in blocking) or "- (see summary)")
        + ("\n\n### Nitpicks\n" + "\n".join(f"- {item}" for item in nitpicks) if nitpicks else ""),
    )
    gh(["pr", "comment", str(args.pr), "--body-file", "-"], input_text=body)
    log(f"PR #{args.pr} requested changes (strike {cycles}/{retries})")
    return EXIT_OK


def cmd_requeue_stale(args: argparse.Namespace) -> int:
    config = load_config()
    minutes = int(args.minutes or config["governance"].get("lease_minutes", 45))
    cutoff = now()
    requeued: list[str] = []
    with queue_txn(config) as queue:
        for task in queue.tasks():
            if task.get("status") not in RUNNING_STATES:
                continue
            lease = parse_iso(task.get("lease_until"))
            if lease and lease < cutoff and (cutoff - lease) > timedelta(0):
                task["status"] = "changes_requested" if task.get("branch") else "pending"
                task["lease_until"] = None
                queue.touch(task, f"lease expired after {minutes} minute(s); requeued")
                requeued.append(task["id"])
    log(f"requeued {len(requeued)} stale task(s): {requeued}")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    with queue_txn(config) as queue:
        tasks = queue.tasks()
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.get("status", "unknown")] = counts.get(task.get("status", "unknown"), 0) + 1
        summary = {
            "queue": str(queue_path(config).relative_to(ROOT)),
            "master_spec_issue": queue.data.get("master_spec_issue"),
            "total_tasks": len(tasks),
            "counts": counts,
            "caps": {
                "max_concurrency": config["governance"]["max_concurrency"],
                "max_open_prs": config["governance"].get("max_open_prs"),
                "review_retries": config["governance"]["review_retries"],
                "max_depth": config["governance"]["max_depth"],
            },
        }
    print(json.dumps(summary, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai_bridge.py", description="Multi-agent queue orchestration engine")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="PM decomposes a master spec issue into the queue")
    plan.add_argument("--issue", type=int, required=True)
    plan.add_argument("--force", action="store_true", help="plan again even if the issue was already planned")
    plan.add_argument("--dry-run", action="store_true")
    plan.set_defaults(func=cmd_plan)

    claim = sub.add_parser("claim", help="claim queued tasks up to the concurrency cap")
    claim.add_argument("--limit", type=int, default=2)
    claim.add_argument("--github-output", default="", help="path to GITHUB_OUTPUT for matrix/count")
    claim.set_defaults(func=cmd_claim)

    run = sub.add_parser("run", help="execute one claimed task as a worker")
    run.add_argument("--task-id", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    review = sub.add_parser("review", help="review a PR after deterministic CI passed")
    review.add_argument("--pr", type=int, required=True)
    review.add_argument("--dry-run", action="store_true")
    review.set_defaults(func=cmd_review)

    requeue = sub.add_parser("requeue-stale", help="return expired worker leases to the queue")
    requeue.add_argument("--minutes", type=int, default=0)
    requeue.set_defaults(func=cmd_requeue_stale)

    status = sub.add_parser("status", help="print queue status")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BudgetHalted as exc:
        log(f"BUDGET HALT: {exc}")
        return exc.exit_code
    except ProtectedPathError as exc:
        log(f"PROTECTED PATH: {exc}")
        return exc.exit_code
    except CiNotGreen as exc:
        log(f"CI GATE: {exc}")
        return exc.exit_code
    except BridgeError as exc:
        log(f"ERROR: {exc}")
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
