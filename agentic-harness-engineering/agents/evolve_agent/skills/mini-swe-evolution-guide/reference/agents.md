# Mini-SWE-agent Agents

The candidate control-flow surface is `workspace/src/minisweagent/agents/`.

## DefaultAgent

`DefaultAgent` is the non-interactive execution loop used by the Harbor bridge. It renders the system and instance templates, calls the model, executes parsed actions through the injected environment, records messages, and saves the native trajectory. Its configurable fields are defined by `AgentConfig` in `agents/default.py`.

Use this surface only when the evidence identifies a native control-flow problem. Examples include an incorrect retry, state, parsing, completion, or trajectory-handling behavior. Do not change a model, provider, budget, transport, task environment, or verifier field; those are fixed outside the candidate workspace.

## InteractiveAgent

`InteractiveAgent` extends `DefaultAgent` for local human-confirmation modes. Harbor evaluation fixes Mini to unattended `yolo` behavior, so changes that only affect interactive prompting are not an evaluation improvement unless the evidence shows that they also affect the evaluated control flow.

## Adding a Native Agent Helper

Create agent helpers only under `workspace/src/minisweagent/agents/`. Creating a file is NOT enough — import it through Mini-SWE-agent's existing agent control flow. Missing that integration means the evaluated candidate will not execute the helper. Model-visible tool schemas and E2B command builders belong under `workspace/src/minisweagent/models/utils/` and must be registered in `tool_registry.py`.

After editing Python, ensure Python imports remain valid after editing `.py` files and verify Python syntax after editing `.py` files.
