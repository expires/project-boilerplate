from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .board import AGENTS_DIRNAME, BoardError, OversizeError, log

CALLS_THIS_RUN = 0


def reset_call_counter() -> None:
    global CALLS_THIS_RUN
    CALLS_THIS_RUN = 0


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_route(config: dict[str, Any], role: str) -> dict[str, Any]:
    roles = config.get("roles", {})
    if role not in roles:
        raise BoardError(f"unknown role '{role}'")
    route = roles[role]
    provider_name = config.get("provider", {}).get("name", "economy")
    provider = config.get("provider", {})
    base_url = os.environ.get(provider.get("base_url_env", ""), "") or provider.get("base_url_default", "")
    api_key = os.environ.get(provider.get("api_key_env", ""), "")
    model = os.environ.get(route.get("model_env", ""), "") or route.get("model_default", "")
    return {
        "route": route,
        "provider_name": provider_name,
        "provider": provider,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model,
    }


def usage_path(root: Path) -> Path:
    return Path(root) / AGENTS_DIRNAME / "usage.json"


def load_usage(root: Path) -> dict[str, Any]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    path = usage_path(root)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if data.get("month") == month:
                return data
        except json.JSONDecodeError:
            pass
    return {"month": month, "estimated_usd": 0.0, "calls": 0, "roles": {}}


def save_usage(root: Path, data: dict[str, Any]) -> None:
    path = usage_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def check_budget(config: dict[str, Any], root: Path) -> None:
    budget = float(config.get("cost_controls", {}).get("monthly_budget_usd", 0) or 0)
    if budget <= 0:
        return
    usage = load_usage(root)
    spent = float(usage.get("estimated_usd", 0.0))
    if spent >= budget and config.get("cost_controls", {}).get("halt_on_budget_exceeded", True):
        raise BoardError(f"monthly budget reached (${spent:.4f} >= ${budget:.2f}); halting")
    warn = config.get("cost_controls", {}).get("warn_at_percent", 80)
    if spent >= budget * float(warn) / 100:
        log(f"budget warning: ${spent:.4f} of ${budget:.2f}")


def record_usage(config: dict[str, Any], root: Path, role: str, provider_name: str, usage: dict[str, Any]) -> None:
    data = load_usage(root)
    pricing = config.get("pricing", {}).get(provider_name, {})
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    cost = (
        prompt_tokens / 1000 * float(pricing.get("input_per_1k_usd", 0))
        + completion_tokens / 1000 * float(pricing.get("output_per_1k_usd", 0))
    )
    data["estimated_usd"] = round(float(data.get("estimated_usd", 0.0)) + cost, 6)
    data["calls"] = int(data.get("calls", 0)) + 1
    roles = data.setdefault("roles", {})
    bucket = roles.setdefault(role, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
    bucket["calls"] += 1
    bucket["prompt_tokens"] += prompt_tokens
    bucket["completion_tokens"] += completion_tokens
    save_usage(root, data)


def call_llm(
    config: dict[str, Any],
    root: Path,
    role: str,
    system_prompt: str,
    user_prompt: str,
    dry_run: bool = False,
    max_output_tokens: int | None = None,
) -> str:
    global CALLS_THIS_RUN
    route_info = resolve_route(config, role)
    route = route_info["route"]
    provider = route_info["provider"]
    if dry_run:
        log(f"[dry-run] role={role} model={route_info['model'] or 'unset'}")
        return '{"dry_run": true}'
    controls = config.get("cost_controls", {})
    max_calls = int(controls.get("max_llm_calls_per_tick", controls.get("max_llm_calls_per_run", 60)))
    if CALLS_THIS_RUN >= max_calls:
        raise BoardError(f"max_llm_calls_per_tick reached ({max_calls})")
    if not route_info["base_url"] or not route_info["api_key"] or not route_info["model"]:
        raise BoardError(
            f"role '{role}' endpoint not configured: set "
            f"{provider.get('base_url_env')} (optional), {provider.get('api_key_env')}, {route.get('model_env')}"
        )
    max_input = int(config.get("cost_controls", {}).get("max_input_chars", 48000))
    if len(system_prompt) + len(user_prompt) > max_input:
        user_prompt = user_prompt[: max(0, max_input - len(system_prompt))]
        log("input truncated to cost_controls.max_input_chars")
    check_budget(config, root)
    max_tokens = min(
        int(route.get("max_output_tokens", 4000)),
        int(config.get("cost_controls", {}).get("max_output_tokens_per_call", 8192)),
    )
    if max_output_tokens:
        max_tokens = int(max_output_tokens)
    payload = {
        "model": route_info["model"],
        "temperature": route.get("temperature", 0.2),
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        route_info["base_url"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {route_info['api_key']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(route.get("timeout_seconds", 180))) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise BoardError(f"LLM HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise BoardError(f"LLM connection failed: {exc.reason}") from exc
    choice = (body.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise OversizeError(
            "response truncated at max_output_tokens — split the card into smaller tasks or raise the cap"
        )
    text = choice.get("message", {}).get("content", "")
    if not text:
        raise BoardError("LLM returned empty content")
    CALLS_THIS_RUN += 1
    record_usage(config, root, role, route_info["provider_name"], body.get("usage", {}))
    return text


def extract_json(text: str) -> Any:
    candidates = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL) + [text]
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for match in re.finditer(r"[\[{]", candidate):
            start = match.start()
            opener = candidate[start]
            closer = "}" if opener == "{" else "]"
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
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(candidate[start : index + 1])
                        except json.JSONDecodeError:
                            break
    raise BoardError("model response did not contain valid JSON")


def sanitize(config: dict[str, Any], text: str | None) -> str:
    if not text:
        return ""
    if not config.get("blind_collaboration", {}).get("enabled", True):
        return text
    result = text
    for term in config.get("blind_collaboration", {}).get("strip_identity_terms", []):
        result = re.sub(re.escape(term), "agent", result, flags=re.IGNORECASE)
    return result
