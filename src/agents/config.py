from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .board import AGENTS_DIRNAME, COLUMNS, Board

GITIGNORE_BLOCK = """# --- agent-board runtime state (board cards are committed; these are not) ---
.agents/worktrees/
.agents/logs/
.agents/usage.json
.agents/runner.log
.agents/runner.pid
.agents/board/.lock
.agents/board/**/.tmp-*
"""

GITIGNORE_TEMPLATE = (
    """# --- Secrets / environment ---
.env
.env.*
!.env.example
**/secrets/
*.pem
*.key
*.p12
*.pfx

"""
    + GITIGNORE_BLOCK
    + """
# --- Python ---
__pycache__/
*.py[cod]
.venv/
venv/
*.egg-info/
.pytest_cache/
.ruff_cache/
.mypy_cache/

# --- Node ---
node_modules/
dist/
build/
coverage/
.next/

# --- OS / editors ---
.DS_Store
Thumbs.db
.idea/
*.swp
"""
)

PROJECT_TEMPLATE = """# Project brief

Written by the Architect during `agents init`. Keep it short and factual.

## Purpose
(what we are building and why)

## Stack
(languages, frameworks, package manager)

## Conventions
- (naming, layout, error handling, commit style)

## Definition of done
- (what a task must satisfy before it can be reviewed)

## Protected paths
- (files the agents must never touch)
"""

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "provider": {
        "name": "economy",
        "base_url_env": "AI_ECONOMY_BASE_URL",
        "base_url_default": "https://api.deepseek.com/v1",
        "api_key_env": "AI_ECONOMY_API_KEY",
    },
    "roles": {
        "pm": {
            "model_env": "PM_MODEL",
            "model_default": "deepseek-chat",
            "temperature": 0.2,
            "max_output_tokens": 4000,
            "timeout_seconds": 180,
        },
        "worker": {
            "model_env": "WORKER_MODEL",
            "model_default": "deepseek-coder",
            "temperature": 0.1,
            "max_output_tokens": 8000,
            "timeout_seconds": 300,
        },
        "reviewer": {
            "model_env": "REVIEWER_MODEL",
            "model_default": "deepseek-chat",
            "temperature": 0.0,
            "max_output_tokens": 3000,
            "timeout_seconds": 180,
        },
    },
    "routes": ["backend", "frontend", "infra", "test", "docs"],
    "governance": {
        "max_concurrency": 2,
        "max_depth": 1,
        "review_retries": 3,
        "max_task_attempts": 2,
        "max_tasks_per_spec": 12,
        "lease_minutes": 45,
        "poll_seconds": 5,
        "decompose_specs": False,
    },
    "cost_controls": {
        "monthly_budget_usd": 25.0,
        "halt_on_budget_exceeded": True,
        "max_llm_calls_per_run": 12,
        "max_output_tokens_per_call": 8000,
        "max_input_chars": 48000,
        "max_diff_lines": 600,
    },
    "context": {
        "max_files_per_task": 8,
        "protected_paths": [
            f"{AGENTS_DIRNAME}/**",
            ".git/**",
            ".env",
            ".env.*",
            "**/secrets/**",
            "**/*.pem",
            "**/id_rsa*",
        ],
        "exclude_globs": [
            "**/node_modules/**",
            "**/.git/**",
            "**/dist/**",
            "**/build/**",
            "**/__pycache__/**",
            "**/.venv/**",
        ],
    },
    "verify": {
        "enabled": False,
        "commands": [],
    },
    "vcs": {
        "base_branch": "main",
        "branch_prefix": "agent/",
        "commit_prefix": "feat(agent)",
    },
    "pricing": {
        "economy": {
            "input_per_1k_usd": 0.00014,
            "output_per_1k_usd": 0.00028,
        }
    },
    "blind_collaboration": {
        "enabled": True,
        "strip_identity_terms": [
            "gpt",
            "chatgpt",
            "openai",
            "anthropic",
            "claude",
            "gemini",
            "deepseek",
            "llama",
            "mistral",
            "qwen",
            "grok",
            "copilot",
        ],
    },
}


def load_config(root: Path) -> dict[str, Any]:
    path = Path(root) / AGENTS_DIRNAME / "config.json"
    if not path.is_file():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return json.loads(path.read_text())


def initial_usage() -> dict[str, Any]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return {"month": month, "estimated_usd": 0.0, "calls": 0, "roles": {}}


def scaffold(root: Path, project_name: str = "") -> Board:
    agents = Path(root) / AGENTS_DIRNAME
    board = Board(root)
    for column in COLUMNS:
        board.column_dir(column).mkdir(parents=True, exist_ok=True)
    for name in ("specs", "logs", "worktrees"):
        (agents / name).mkdir(parents=True, exist_ok=True)

    config_path = agents / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")

    project_path = agents / "project.md"
    if not project_path.exists():
        header = f"# Project brief: {project_name}\n\n" if project_name else ""
        project_path.write_text(header + PROJECT_TEMPLATE.split("\n", 1)[1])

    usage_path = agents / "usage.json"
    if not usage_path.exists():
        usage_path.write_text(json.dumps(initial_usage(), indent=2) + "\n")

    env_path = Path(root) / ".env"
    if not env_path.exists():
        env_path.write_text(
            "# agent-board: DeepSeek key used by the PM, workers, and reviewer.\n"
            "AI_ECONOMY_API_KEY=\n"
        )

    ensure_gitignore(Path(root))

    return board


def ensure_gitignore(root: Path) -> Path:
    path = Path(root) / ".gitignore"
    if not path.exists():
        path.write_text(GITIGNORE_TEMPLATE)
        return path
    existing = path.read_text()
    if ".agents/worktrees/" in existing:
        return path
    separator = "" if existing.endswith("\n") else "\n"
    path.write_text(existing + separator + "\n" + GITIGNORE_BLOCK)
    return path
