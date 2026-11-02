# Mini-SWE-agent Configuration

The candidate configuration loaded by the Harbor bridge is `workspace/src/minisweagent/config/mini.yaml`.

## Editable Surface

Use the prompt and strategy fields in `mini.yaml` for behavioral rules, workflow guidance, task-completion strategy, tool-use guidance, and error-recovery instructions. Preserve the native Mini-SWE-agent YAML structure.

## Fixed Evaluation Fields

The fixed bridge resolves the candidate configuration together with an evaluation lock and runtime values. The candidate may not change model identity, provider endpoint, sampling parameters, step/cost/wall-time limits, shell timeout, E2B transport, Harbor task selection, or verifier behavior.

The workspace policy enforces this by comparing the candidate with H0 and locking the relevant `model`, `agent`, and `environment` fields. Always use the validation script after a configuration edit.

## Configuration Loading

Later configuration layers take precedence during the bridge's recursive merge. A candidate prompt change remains effective, while the locked evaluation fields are restored to the fixed values. The audited result for a trial is stored in `agent/ahe_candidate_config.resolved.yaml`.

Tool registration is not configured through `mini.yaml`. The fixed LiteLLM provider reads `get_tool_schemas()` from the candidate `models/utils/tool_registry.py` through the fixed host audit guard, while `actions_toolcall.py` converts returned calls into E2B-backed actions. Observation templates may use `tool_name` and `tool_arguments` in addition to `output` and the existing runtime template variables.
