from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import prompts
from .board import BoardError, Card
from .llm import call_llm, extract_json, sanitize
from .validate import is_protected

LLM = Callable[[dict[str, Any], Path, str, str, str], str]


def default_llm(config: dict[str, Any], root: Path, role: str, system: str, user: str) -> str:
    return call_llm(config, root, role, system, user)


def _fill(template: str, **values: str) -> str:
    result = template
    for key, value in values.items():
        result = result.replace(f"__{key}__", value)
    return result


def _validate_tasks(config: dict[str, Any], tasks: Any) -> list[dict[str, Any]]:
    if not isinstance(tasks, list) or not tasks:
        raise BoardError("PM returned no tasks")
    max_tasks = int(config["governance"]["max_tasks_per_spec"])
    if len(tasks) > max_tasks:
        raise BoardError(f"PM returned {len(tasks)} tasks; cap is {max_tasks}")
    max_files = int(config["context"]["max_files_per_task"])
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for item in tasks:
        if not isinstance(item, dict):
            raise BoardError("each task must be an object")
        key = str(item.get("key") or "").strip()
        title = str(item.get("title") or "").strip()
        files = [str(path) for path in (item.get("files") or []) if str(path).strip()]
        if not key or not title:
            raise BoardError("each task needs a 'key' and 'title'")
        if key in keys:
            raise BoardError(f"duplicate task key '{key}'")
        keys.add(key)
        if not files:
            raise BoardError(f"task '{key}' lists no files")
        if len(files) > max_files + 2:
            raise BoardError(f"task '{key}' lists {len(files)} files; cap is {max_files}")
        criteria = [str(c) for c in (item.get("acceptance_criteria") or []) if str(c).strip()]
        depends = [str(d) for d in (item.get("depends_on") or []) if str(d).strip()]
        normalized.append(
            {
                "key": key,
                "title": title,
                "route": str(item.get("route") or "backend"),
                "files": files,
                "acceptance_criteria": criteria,
                "depends_on": depends,
            }
        )
    for task in normalized:
        for dep in task["depends_on"]:
            if dep not in keys:
                raise BoardError(f"task '{task['key']}' depends on unknown '{dep}'")
        for path in task["files"]:
            if is_protected(config, path):
                raise BoardError(f"task '{task['key']}' targets protected path '{path}'")
    _assert_acyclic(normalized)
    return normalized


def _assert_acyclic(tasks: list[dict[str, Any]]) -> None:
    graph = {task["key"]: list(task["depends_on"]) for task in tasks}
    visited: set[str] = set()

    def visit(node: str, stack: set[str]) -> None:
        if node in visited:
            return
        if node in stack:
            raise BoardError(f"dependency cycle through '{node}'")
        stack.add(node)
        for dep in graph.get(node, []):
            visit(dep, stack)
        stack.discard(node)
        visited.add(node)

    for key in graph:
        visit(key, set())


def pm_decompose(
    config: dict[str, Any],
    root: Path,
    spec_text: str,
    spec_name: str,
    llm: LLM | None = None,
) -> list[dict[str, Any]]:
    system = _fill(
        prompts.PM_SYSTEM,
        MAX_TASKS=str(config["governance"]["max_tasks_per_spec"]),
        MAX_FILES=str(config["context"]["max_files_per_task"]),
        ROUTES=", ".join(config.get("routes", [])),
        PROTECTED=", ".join(config["context"].get("protected_paths", [])),
    )
    user = _fill(prompts.PM_USER, NAME=spec_name, SPEC=spec_text)
    raw = (llm or default_llm)(config, root, "pm", system, user)
    payload = extract_json(raw)
    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    return _validate_tasks(config, tasks)


def worker_propose(
    config: dict[str, Any],
    root: Path,
    card: Card,
    context: str,
    feedback: str,
    llm: LLM | None = None,
) -> dict[str, Any]:
    criteria = "\n".join(f"- {item}" for item in card.acceptance_criteria) or "- (none)"
    files = "\n".join(f"- {item}" for item in card.files_hint) or "- (choose as needed)"
    user = _fill(
        prompts.WORKER_USER,
        ID=card.id,
        TITLE=card.title,
        SPEC=card.spec or card.body,
        CRITERIA=criteria,
        FILES=files,
        CONTEXT=context or "(no existing files)",
        FEEDBACK=feedback or "(none)",
    )
    raw = (llm or default_llm)(config, root, "worker", prompts.WORKER_SYSTEM, user)
    payload = extract_json(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list) or not payload["files"]:
        raise BoardError("worker response must contain a non-empty 'files' list")
    return payload


def resolve_conflicts(
    config: dict[str, Any],
    root: Path,
    base: str,
    branch: str,
    conflicts: dict[str, str],
    llm: LLM | None = None,
) -> dict[str, str]:
    if not conflicts:
        return {}
    chunks = [f"### {path}\n{content}" for path, content in conflicts.items()]
    system = _fill(prompts.CONFLICT_SYSTEM, BRANCH=branch, BASE=base)
    user = _fill(prompts.CONFLICT_USER, FILES="\n\n".join(chunks))
    raw = (llm or default_llm)(config, root, "pm", system, user)
    payload = extract_json(raw)
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return {}
    resolved: dict[str, str] = {}
    for item in files:
        if isinstance(item, dict) and item.get("path") and isinstance(item.get("content"), str):
            resolved[str(item["path"])] = item["content"]
    return resolved


def reviewer_verdict(
    config: dict[str, Any],
    root: Path,
    card: Card,
    diff_text: str,
    cycle: int,
    max_cycles: int,
    llm: LLM | None = None,
) -> dict[str, Any]:
    criteria = "\n".join(f"- {item}" for item in card.acceptance_criteria) or "- (none)"
    user = _fill(
        prompts.REVIEWER_USER,
        ID=card.id,
        TITLE=card.title,
        CRITERIA=criteria,
        DIFF=diff_text,
        CYCLE=str(cycle),
        MAX_CYCLES=str(max_cycles),
    )
    raw = (llm or default_llm)(config, root, "reviewer", prompts.REVIEWER_SYSTEM, user)
    payload = extract_json(raw)
    if not isinstance(payload, dict):
        raise BoardError("reviewer response must be an object")
    verdict = str(payload.get("verdict", "request_changes")).strip().lower()
    if verdict not in ("approve", "request_changes"):
        verdict = "request_changes"
    return {
        "verdict": verdict,
        "summary": sanitize(config, str(payload.get("summary", ""))),
        "blocking_issues": [sanitize(config, str(item)) for item in payload.get("blocking_issues", [])],
        "nitpicks": [sanitize(config, str(item)) for item in payload.get("nitpicks", [])],
    }
