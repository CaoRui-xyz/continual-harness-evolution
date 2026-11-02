---
name: mini-swe-evolution-guide
description: Mini-SWE-agent harness evolution reference. Use when evolving the permitted prompt, agent control flow, model utilities, or registered E2B-backed tools. Covers candidate wiring, the fixed provider boundary, and required validation.
---

# Mini-SWE-agent Evolution Guide — Starting Point

The Mini-SWE-agent harness starts from the pinned Mini-SWE-agent v2.4.6 source with its native prompt/configuration, agent control flow, and one registered `bash` tool. Evolution progressively improves those components or adds general tools through evidence-based changes. This guide covers what you can modify, how components are connected, and how to validate them.

## Current Agent Baseline

```text
workspace/src/minisweagent/config/mini.yaml  # Prompt and permitted strategy configuration
workspace/src/minisweagent/agents/           # Native agent control flow
workspace/src/minisweagent/models/utils/      # Tool registry, parsers, builders, observations, and model utilities
workspace/src/minisweagent/models/litellm_model.py  # Fixed provider entry point; read only
workspace/src/minisweagent/models/host_guard.py  # Fixed host audit boundary; read only
```

Everything below is **available to add** during evolution.

## Validation

After any change, **always validate**:

```bash
python evolve_agent/skills/mini-swe-evolution-guide/scripts/validate_workspace.py \
  --workspace workspace \
  --policy ${AHE_ROOT}/configs/mini_swe/workspace_policy.yaml \
  --evaluation-lock ${AHE_ROOT}/configs/mini_swe/evaluation_lock.yaml \
  --runtime-python ${META_HARBOR_RUNNER_PYTHON} \
  --harbor-source-dir ${META_HARBOR_SOURCE_DIR} \
  --baseline-source ${MINI_SWE_SEED_DIR}
```

The validator compares the actual filesystem with H0, rejects Git index hiding flags, symbolic links, changes outside the permitted paths, changes to the fixed provider/guard seed, and changes to locked model, budget, or environment fields in `config/mini.yaml`. It also checks Python syntax, imports, import-time host effects, Mini configuration schemas, Jinja templates, Draft 7 tool schemas, exact registry/schema correspondence, schema-valid smoke arguments, statically inspectable candidate runtime utilities and deterministic command builders, host-side effects, parser/observation behavior, and E2B action conversion without calling the model or creating an E2B sandbox.

---

## Editing the Prompt / Strategy Configuration

Edit `workspace/src/minisweagent/config/mini.yaml` when evidence points to behavioral rules, workflow guidance, task-completion strategy, tool-use guidance, or error-recovery instructions. Preserve the native Mini-SWE-agent YAML structure and all locked fields.

Do not replace a demonstrated control-flow defect with prompt-only advice. If a prompt change fails across iterations, rollback the ineffective change and evaluate whether the root cause belongs in the native agent control flow.

---

## Editing Agent Control Flow

Edit `workspace/src/minisweagent/agents/` only when trajectories show a native Mini-SWE-agent control-flow, retry, state, parsing, or completion-handling defect that prompt/configuration changes cannot address.

New helpers may only be created under `workspace/src/minisweagent/agents/`. Creating a file is NOT enough — import it through Mini-SWE-agent's existing agent control flow. Missing that integration means the evaluated candidate will not execute the helper.

The candidate workspace's `src/` directory is imported by Harbor's fixed bridge at evaluation time. The bridge supplies the E2B environment and freezes the model identity, provider endpoint, sampling parameters, budgets, shell timeout, task selection, and verifier. Do not modify or work around those settings.

---

## Adding or Editing Tools

The fixed `models/litellm_model.py` calls `get_tool_schemas()` from `models/utils/tool_registry.py`. Add every model-visible tool to `TOOL_REGISTRY` as a `ToolRegistration` containing:

1. `schema` — an OpenAI chat-completions function schema whose function name exactly matches the registry key.
2. `build_command` — a pure function that validates structured arguments and returns one non-empty shell command.
3. `smoke_arguments` — safe representative arguments used by the validator to call the builder without executing its command.

All tools use the same execution boundary:

```text
model tool call
→ models/utils/actions_toolcall.py
→ tool_registry.build_tool_action()
→ bash: {command, tool_call_id}; added tools: {command, tool_call_id, tool_name, tool_arguments}
→ DefaultAgent.execute_actions()
→ HarborMiniEnvironment
→ E2B task sandbox
```

Creating a schema or builder alone is NOT enough. Register it in `TOOL_REGISTRY`, preserve the action metadata, and ensure the command works from the task image's working directory. Builders must not execute subprocesses, access the host filesystem, use the network, or read host environment variables.

Edit other files under `models/utils/` only when evidence points to cache, multimodal, parsing, observation, or message utility behavior. The original `retry.py` is fixed because it reads a controlled host environment variable. Candidate runtime utilities are called on the Harbor host through a fixed audit guard and must remain pure with respect to host files, environment, processes, and network. Do not use the editable surface to change the provider, model identity, endpoint, sampling parameters, model-call count, cost accounting, or host/E2B boundary.

---

## Candidate Boundary

Modify only:

```text
workspace/src/minisweagent/agents/**
workspace/src/minisweagent/config/mini.yaml
workspace/src/minisweagent/models/utils/**
```

Do not modify `models/litellm_model.py`, `models/__init__.py`, other provider implementations, package metadata, tests, environments, runners, the Harbor bridge, E2B transport, datasets, task environments, or verifiers. Do not create framework-specific candidate components outside the permitted Mini-SWE-agent paths.

---

## Reference Docs

Read only the reference that matches the demonstrated failure:

| File | Content |
|------|---------|
| `reference/agents.md` | Mini-SWE-agent control flow and native helper integration. |
| `reference/configuration.md` | Candidate `mini.yaml`, fixed evaluation fields, and configuration loading. |
| `reference/tools.md` | Tool registry schema, command builder, parser, and E2B execution contract. |
| `reference/evaluation.md` | Harbor/E2B boundary and read-only trial evidence. |
