"""Parse actions & format observations with toolcalls"""

import json
import time

from jinja2 import StrictUndefined, Template

from minisweagent.exceptions import FormatError
from minisweagent.models.utils.openai_multimodal import expand_multimodal_content
from minisweagent.models.utils.tool_registry import BASH_TOOL, TOOL_REGISTRY, build_tool_action


def parse_toolcall_actions(
    tool_calls: list, *, format_error_template: str, template_kwargs: dict | None = None
) -> list[dict]:
    """Parse tool calls from the response. Raises FormatError if unknown tool or invalid args.

    ``template_kwargs`` are extra variables exposed to ``format_error_template`` (e.g.
    ``{"finish_reason": ...}`` so a template can distinguish a real format mistake from a
    ``max_tokens`` truncation).
    """
    template_kwargs = template_kwargs or {}
    if not tool_calls:
        raise FormatError(
            {
                "role": "user",
                "content": Template(format_error_template, undefined=StrictUndefined).render(
                    error="No tool calls found in the response. Every response MUST include at least one tool call.",
                    actions=[],
                    has_tool_calls=False,
                    **template_kwargs,
                ),
                "extra": {"interrupt_type": "FormatError"},
            }
        )
    actions = []
    for tool_call in tool_calls:
        error_msg = ""
        args = {}
        try:
            args = json.loads(tool_call.function.arguments)
        except Exception as error:
            error_msg = f"Error parsing tool call arguments: {error}."
        tool_name = tool_call.function.name
        if tool_name not in TOOL_REGISTRY:
            error_msg += f"Unknown tool '{tool_call.function.name}'."
        if (tool_name not in TOOL_REGISTRY or tool_name == "bash") and (not isinstance(args, dict) or "command" not in args):
            error_msg += "Missing 'command' argument in bash tool call."
        if tool_name != "bash" and tool_name in TOOL_REGISTRY and not isinstance(args, dict):
            error_msg += f"Tool '{tool_name}' arguments must decode to an object."
        action = None
        if not error_msg:
            try:
                action = build_tool_action(tool_name, args, tool_call.id)
            except Exception as error:
                error_msg = str(error)
        if error_msg:
            raise FormatError(
                {
                    "role": "user",
                    "content": Template(format_error_template, undefined=StrictUndefined).render(
                        actions=[], error=error_msg.strip(), has_tool_calls=True, **template_kwargs
                    ),
                    "extra": {"interrupt_type": "FormatError"},
                }
            )
        actions.append(action)
    return actions


def format_toolcall_observation_messages(
    *,
    actions: list[dict],
    outputs: list[dict],
    observation_template: str,
    template_vars: dict | None = None,
    multimodal_regex: str = "",
) -> list[dict]:
    """Format execution outputs into tool result messages."""
    not_executed = {"output": "", "returncode": -1, "exception_info": "action was not executed"}
    padded_outputs = outputs + [not_executed] * (len(actions) - len(outputs))
    results = []
    for action, output in zip(actions, padded_outputs):
        content = Template(observation_template, undefined=StrictUndefined).render(
            output=output,
            tool_name=action.get("tool_name", "bash"),
            tool_arguments=action.get("tool_arguments", {}),
            **(template_vars or {}),
        )
        msg = {
            "content": content,
            "extra": {
                "raw_output": output.get("output", ""),
                "returncode": output.get("returncode"),
                "timestamp": time.time(),
                "exception_info": output.get("exception_info"),
                **({"tool_name": action["tool_name"], "tool_arguments": action.get("tool_arguments", {})} if "tool_name" in action else {}),
                **output.get("extra", {}),
            },
        }
        if "tool_call_id" in action:
            msg["tool_call_id"] = action["tool_call_id"]
            msg["role"] = "tool"
        else:
            msg["role"] = "user"
        if multimodal_regex:
            msg = expand_multimodal_content(msg, pattern=multimodal_regex)
        results.append(msg)
    return results
