# Mini-SWE-agent Tool Registry

The model-visible tool surface is `workspace/src/minisweagent/models/utils/tool_registry.py`. H0 registers only `bash`, preserving the original Mini-SWE-agent capability. Add or modify tools only when evaluation evidence identifies a general capability, argument, parsing, or observation failure.

## Registration Contract

Each entry in `TOOL_REGISTRY` must be a `ToolRegistration`:

```python
TOOL_REGISTRY["tool_name"] = ToolRegistration(
    schema={
        "type": "function",
        "function": {
            "name": "tool_name",
            "description": "When and why the model should use this tool.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    build_command=build_tool_command,
    smoke_arguments={},
)
```

The registry key and `schema["function"]["name"]` must match exactly. Descriptions must state the intended use and important limitations. Parameter schemas must describe an object because the fixed parser passes a mapping to the builder. Parameters must be valid Draft 7 JSON Schema, and `smoke_arguments` must validate against that schema. `get_tool_schemas()` must return the registry schemas exactly in insertion order.

## Command Builder

`build_command(arguments)` deterministically validates the structured arguments and returns one non-empty shell command. The command is not executed by the builder. It is placed in a Mini action and later sent through `HarborMiniEnvironment` to the task's E2B sandbox.

Use `shlex.quote()` for untrusted string arguments. Do not construct shell commands by directly interpolating arbitrary model strings. Reject invalid values with `ValueError`; `actions_toolcall.py` converts the error into a recoverable Mini `FormatError`.

Modules must also remain side-effect free while they are imported on the Harbor host. Builders and any Python helpers they call must remain statically inspectable. They must not reference `subprocess`, operating-system or filesystem APIs, network libraries, environment-variable APIs, dynamic import/execution, or time/random sources. Those operations would either run on the Harbor host or make identical arguments produce irreproducible commands.

## Smoke Arguments

`smoke_arguments` must be a safe representative dictionary accepted by the builder. Validation calls the builder twice with identical arguments, blocks common host filesystem, environment, network, and subprocess APIs during those calls, and never executes the returned command. Use harmless values such as a nonexistent relative filename or `true`; do not use a command that relies on benchmark-specific files or solutions.

## Parser and Observations

`models/utils/actions_toolcall.py` parses every registered model tool call, including `bash`, and delegates to `build_tool_action()`. The H0 `bash` action intentionally preserves Mini v2.4.6's exact interface:

```python
{
    "command": "...",
    "tool_call_id": "...",
}
```

An added registered tool action contains:

```python
{
    "command": "...",
    "tool_call_id": "...",
    "tool_name": "...",
    "tool_arguments": {...},
}
```

Preserve these fields when changing parsing or observation formatting. `command` is executed in E2B; metadata on added tools aligns the provider response, model observation, trajectory, and AHE tool statistics. Observation templates may use `tool_name` and `tool_arguments`; for H0 bash they resolve to `bash` and `{}` without changing the stored bash action.

## Fixed Provider Boundary

`workspace/src/minisweagent/models/litellm_model.py` and `workspace/src/minisweagent/models/host_guard.py` are fixed. The provider reads registry schemas and calls candidate parser, observation, retry, cache, multimodal, and message utilities through the fixed thread-scoped host audit guard. Do not modify either file. Model identity, endpoint, sampling parameters, API request count, response handling, and cost tracking remain part of the fixed evaluation boundary.
