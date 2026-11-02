"""Provider-neutral tool-using proposer for Mini Meta-Harness.

The original TB2 example shells out to Claude Code.  This backend keeps the
same synchronous ``run`` contract and session metadata, but uses LiteLLM so
DeepSeek and other OpenAI-compatible providers can drive the proposer loop.
The proposer receives one host tool (``bash``) and the controller remains the
owner of validation, smoke tests, Harbor, E2B, and frontier updates.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


MAX_TOOL_OUTPUT = 20_000


@dataclass
class SessionResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    token_usage: dict[str, int] = field(
        default_factory=lambda: {"input_tokens": 0, "output_tokens": 0}
    )
    session_id: str = ""
    command: list[str] = field(default_factory=list)
    tool_calls: int = 0

    def show(self) -> None:
        """Print a compact provider-independent completion summary."""
        if self.stdout:
            print(self.stdout[-4_000:])
        if self.stderr:
            print(self.stderr[-2_000:])
        print(
            "  proposer: "
            f"exit={self.exit_code} tools={self.tool_calls} "
            f"{self.token_usage.get('input_tokens', 0)}in/"
            f"{self.token_usage.get('output_tokens', 0)}out "
            f"${self.cost_usd:.4f} {self.duration_seconds:.1f}s"
        )


def _load_skills(skills: list[str] | None) -> str:
    sections: list[str] = []
    for value in skills or []:
        path = Path(value)
        # Keep the same directory convention as the TB2 Claude wrapper:
        # callers pass a skill directory and its instructions live in
        # ``SKILL.md``.  Accepting a file as well keeps this backend useful for
        # one-off prompt fragments and preserves the public ``run`` contract.
        if path.is_dir():
            path = path / "SKILL.md"
        if path.is_file():
            sections.append(f"## Skill: {path.stem}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(sections)


def _message_dict(message: Any) -> dict[str, Any]:
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    if hasattr(message, "dict"):
        return message.dict(exclude_none=True)
    if isinstance(message, dict):
        return message
    return {"role": "assistant", "content": str(message)}


def _tool_call_parts(tool_call: Any) -> tuple[str, str, str]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function") or {}
        return (
            str(tool_call.get("id") or "tool-call"),
            str(function.get("name") or ""),
            str(function.get("arguments") or "{}"),
        )
    function = getattr(tool_call, "function", None)
    return (
        str(getattr(tool_call, "id", "tool-call")),
        str(getattr(function, "name", "")),
        str(getattr(function, "arguments", "{}")),
    )


def _usage(response: Any) -> tuple[int, int, float]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0, 0.0

    def value(name: str) -> int:
        raw = usage.get(name, 0) if isinstance(usage, dict) else getattr(usage, name, 0)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    prompt = value("prompt_tokens") or value("input_tokens")
    completion = value("completion_tokens") or value("output_tokens")
    raw_cost = getattr(response, "_hidden_params", {}) or {}
    if isinstance(raw_cost, dict):
        cost = raw_cost.get("response_cost", 0.0)
    else:
        cost = 0.0
    try:
        return prompt, completion, float(cost or 0.0)
    except (TypeError, ValueError):
        return prompt, completion, 0.0


def _api_key(model: str, api_key_env: str | None) -> tuple[str | None, str]:
    inferred = api_key_env or ""
    if not inferred:
        lowered = model.lower()
        if "deepseek" in lowered:
            inferred = "DEEPSEEK_API_KEY"
        elif "anthropic" in lowered or "claude" in lowered:
            inferred = "ANTHROPIC_API_KEY"
        elif "openai" in lowered:
            inferred = "OPENAI_API_KEY"
        else:
            inferred = "LLM_API_KEY"
    key = os.environ.get(inferred) or os.environ.get("LLM_API_KEY")
    return key, inferred


def _run_bash(command: str, cwd: Path, timeout_seconds: int) -> str:
    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        output = (
            f"exit={completed.returncode}\n"
            f"stdout={completed.stdout or ''}\n"
            f"stderr={completed.stderr or ''}"
        )
    except subprocess.TimeoutExpired:
        output = f"command timed out after {timeout_seconds}s"
    except OSError as error:
        output = f"command execution error: {error}"
    return output[-MAX_TOOL_OUTPUT:]


def run(
    prompt: str,
    model: str = "deepseek/deepseek-v4-flash",
    allowed_tools: list[str] | None = None,
    tools: list[str] | None = None,
    cwd: str | None = None,
    log_dir: str | None = None,
    name: str | None = None,
    skills: list[str] | None = None,
    timeout_seconds: int | None = None,
    effort: str | None = None,
    api_base: str | None = None,
    api_key_env: str | None = None,
    max_steps: int = 200,
    max_tokens: int = 16_384,
    command_timeout_seconds: int = 120,
    **model_kwargs: Any,
) -> SessionResult:
    """Run a bounded provider-neutral proposer session.

    ``allowed_tools`` is accepted for compatibility with the TB2 wrapper. The
    backend intentionally exposes only ``bash`` because the model can inspect
    and edit the supplied workspace through ordinary shell commands.
    """
    del allowed_tools, tools, effort
    started = time.monotonic()
    deadline = started + timeout_seconds if timeout_seconds else None
    root = Path(cwd or os.getcwd()).resolve()
    key, key_env = _api_key(model, api_key_env)
    if not key:
        return SessionResult(
            exit_code=2,
            stderr=f"missing proposer API key: set {key_env} or LLM_API_KEY",
            duration_seconds=time.monotonic() - started,
        )

    try:
        import litellm
    except ImportError as error:
        return SessionResult(
            exit_code=2,
            stderr=f"LiteLLM is required for the proposer backend: {error}",
            duration_seconds=time.monotonic() - started,
        )

    skill_text = _load_skills(skills)
    system = (
        "You are the outer proposer for a Meta-Harness Mini-SWE-Agent run. "
        "Use the bash tool to inspect the supplied workspace and run only local "
        "static/unit checks. Implement exactly one general-purpose candidate, "
        "then write the controller-provided pending_eval.json. Do not run Harbor, "
        "E2B, a benchmark, or any full evaluation. Never edit the frozen H0 seed, "
        "runtime lock, evaluation policy, task manifests, or verifier.\n\n"
        f"{skill_text}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    tool_schema = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run a local shell command in the candidate workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tool_schema,
        "tool_choice": "auto",
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "drop_params": True,
    }
    if timeout_seconds:
        kwargs["timeout"] = min(timeout_seconds, 180)
    if api_base:
        kwargs["api_base"] = api_base
    if key:
        kwargs["api_key"] = key
    kwargs.update(model_kwargs)

    result = SessionResult(exit_code=1)
    try:
        for _ in range(max_steps):
            if deadline is not None and time.monotonic() >= deadline:
                result.exit_code = 124
                result.stderr = f"proposer timed out after {timeout_seconds}s"
                break
            response = litellm.completion(**kwargs)
            prompt_tokens, output_tokens, cost = _usage(response)
            result.token_usage["input_tokens"] += prompt_tokens
            result.token_usage["output_tokens"] += output_tokens
            result.cost_usd += cost
            message = response.choices[0].message
            messages.append(_message_dict(message))
            calls = getattr(message, "tool_calls", None) or []
            if not calls:
                result.stdout = str(getattr(message, "content", "") or "")
                result.exit_code = 0
                break
            for call in calls:
                call_id, function_name, raw_arguments = _tool_call_parts(call)
                result.tool_calls += 1
                if function_name != "bash":
                    output = f"unsupported proposer tool: {function_name}"
                else:
                    try:
                        arguments = json.loads(raw_arguments)
                        command = arguments["command"]
                        if not isinstance(command, str) or not command.strip():
                            raise ValueError("bash command must be a non-empty string")
                        remaining = (
                            max(1, int(deadline - time.monotonic()))
                            if deadline is not None
                            else command_timeout_seconds
                        )
                        output = _run_bash(
                            command,
                            root,
                            min(command_timeout_seconds, remaining),
                        )
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                        output = f"invalid bash arguments: {error}"
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": output}
                )
        else:
            result.exit_code = 124
            result.stderr = f"proposer exceeded max_steps={max_steps}"
    except Exception as error:  # provider errors are reported to the controller
        result.exit_code = 1
        result.stderr = f"{type(error).__name__}: {error}"
    result.duration_seconds = time.monotonic() - started

    if log_dir:
        session_dir = Path(log_dir) / (name or time.strftime("%Y%m%d_%H%M%S"))
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "meta.json").write_text(
            json.dumps(
                {
                    **asdict(result),
                    "model": model,
                    "api_base": api_base,
                    "api_key_env": key_env,
                    "cwd": str(root),
                    "prompt": prompt,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return result


SessionResult.__module__ = __name__
