"""将原生 mini-SWE trajectory 只读转换为 AHE/ADB 可读视图。"""

import hashlib
import json
from pathlib import Path
from typing import Any

def write_openai_messages_view(trajectory_path: Path, output_path: Path) -> dict[str, Any]:
    """只复制 Mini `messages`，并记录来源 SHA256；不创建假的 NexAU trace。"""
    raw_bytes = trajectory_path.read_bytes()
    trajectory = json.loads(raw_bytes)
    messages = trajectory.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError(f"Mini trajectory messages 必须是 list: {trajectory_path}")
    view = {
        "trace_id": trajectory.get("instance_id", trajectory_path.stem),
        "messages": messages,
        "source": {
            "path": str(trajectory_path),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "format": trajectory.get("trajectory_format", "mini-swe-agent"),
        },
    }
    output_path.write_text(json.dumps(view, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return view


def extract_behavior_stats(trajectory_path: Path) -> dict[str, Any]:
    """计算模型调用、tool action、退出状态和成本，不依赖 NexAU span。"""
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    messages = trajectory.get("messages", [])
    model_actions = [
        action
        for message in messages
        if isinstance(message, dict)
        for action in message.get("extra", {}).get("actions", [])
        if isinstance(action, dict)
    ]
    info = trajectory.get("info", {}) if isinstance(trajectory.get("info", {}), dict) else {}
    model_stats = info.get("model_stats", {}) if isinstance(info.get("model_stats", {}), dict) else {}
    tool_usage: dict[str, int] = {}
    for action in model_actions:
        tool_name = str(action.get("tool_name", "bash"))
        tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1
    n_tool_errors = 0
    tool_errors: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        extra = message.get("extra", {}) if isinstance(message.get("extra", {}), dict) else {}
        return_code = extra.get("returncode")
        exception_info = extra.get("exception_info")
        is_error = (return_code is not None and return_code != 0) or bool(exception_info)
        if not is_error:
            continue
        tool_name = str(extra.get("tool_name", "bash"))
        n_tool_errors += 1
        tool_errors[tool_name] = tool_errors.get(tool_name, 0) + 1
    return {
        "n_llm_calls": int(model_stats.get("api_calls", 0) or 0),
        "n_tool_calls": len(model_actions),
        "tool_usage": tool_usage,
        "n_tool_errors": n_tool_errors,
        "tool_errors": tool_errors,
        "last_tool": str(model_actions[-1].get("tool_name", "bash")) if model_actions else "",
        "return_code": next(
            (message.get("extra", {}).get("returncode") for message in reversed(messages) if isinstance(message, dict) and "returncode" in message.get("extra", {})),
            None,
        ),
        "exit_status": info.get("exit_status", ""),
        "cost": float(model_stats.get("instance_cost", 0.0) or 0.0),
    }
