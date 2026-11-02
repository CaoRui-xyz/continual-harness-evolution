# Harbor Evaluation Boundary

Harbor selects the configured task or dataset, prepares the task environment, manages the E2B sandbox, and runs the task's verifier. The candidate Mini-SWE-agent runs through the fixed host bridge, which imports `workspace/src/` and loads `workspace/src/minisweagent/config/mini.yaml`.

Do not modify the Harbor bridge, E2B transport, dataset, task image, task environment, or verifier. Those are evaluation infrastructure, not candidate harness components.

The fixed provider client, candidate tool builders, schemas, parser, observation formatter, and model utilities execute on the Harbor host. The fixed provider invokes these candidate utilities through a thread-scoped Python audit guard that blocks host file operations, process creation, direct network access, and host-state mutation during the call. Static validation applies the same pure-host boundary recursively to reachable candidate helpers. This is application-level defense, not an OS container; only returned tool commands cross the fixed Mini environment boundary and execute inside E2B.

## Trial Evidence

Use the following read-only artifacts when analysis reports are missing or insufficient:

| Artifact | Purpose |
|----------|---------|
| `agent/mini-swe-agent.trajectory.json` | Original Mini-SWE-agent messages, actions, configuration, and exit status. |
| `agent/ahe_openai_messages.json` | Read-only messages view derived from the original Mini trajectory. |
| `agent/ahe_candidate_config.resolved.yaml` | Audited effective candidate configuration after fixed evaluation layers are applied. |
| `agent/ahe_candidate_tools.json` | Actual model-visible tool schemas, SHA256 identities of candidate `models/utils` Python files, and the fixed provider SHA256. |
| `verifier/reward.txt` | Final reward for the trial. |
| `verifier/test-stdout.txt`, `verifier/test-stderr.txt`, `verifier/report.json` | Verifier evidence when the task provides them. |

Use evidence to identify failure patterns, not to add task-specific logic or hardcoded solutions.
