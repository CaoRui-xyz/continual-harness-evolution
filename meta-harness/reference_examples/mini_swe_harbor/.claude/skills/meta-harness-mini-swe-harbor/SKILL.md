---
name: meta-harness-mini-swe-harbor
description: Run one Meta-Harness candidate proposal for an AHE-compatible Mini-SWE-Agent workspace.
---

# Meta-Harness Mini-SWE Harbor

Run ONE iteration of agent scaffold evolution.

**You do NOT run benchmarks.** You analyze results + failed trajectories,
propose one Mini-SWE-Agent workspace variant, and implement it. The outer loop
(`meta_harness.py`) handles validation, snapshots, Harbor/E2B benchmarking,
scoring and frontier selection.

## CRITICAL CONSTRAINTS

- You MUST produce 1 new agent variant every iteration.
- Do NOT write "the frontier is optimal" or "stop iterating", or abort early.

### Anti-overfitting rules

- **No task-specific hints.** Do not hardcode knowledge about specific tasks. Agents must be general-purpose.
- **Never mention task names** in agent code, prompts, or comments. No references like "if task contains 'async'" or "for polyglot tasks." If your improvement only helps one task, it's too specific.
- **General guidance is OK.** Rules like "back up files before opening them with tools that modify on read" are fine -- they happen to help specific tasks but apply broadly. The test: would this advice be useful to a human developer working on MANY unfamiliar tasks?
- **If in doubt, make it more general.** "Always read eval scripts before submitting" > "Read the grading script for DNA assembly tasks."

## CONTEXT

You are evolving the **Mini-SWE-Agent** scaffold in the controller-provided
workspace. Every such workspace starts from fixed H0. A frontier winner is
never silently promoted as its Git parent. You may inspect a prior frozen
candidate and deliberately copy an allowed-file mechanism into the new
workspace, but never import from, write to, or create a runtime dependency on
a prior candidate. Treat snapshots as read-only evidence.

**The search space is the policy-allowed Mini-SWE-Agent code.** You may improve
agent control flow, prompts/configuration, and permitted tool utilities. The
fixed Harbor Mini bridge loads the workspace through
`CandidateMiniSweAgent`; do not replace its provider/model classes, environment,
runner, dependencies, evaluation policy, task suite/manifest, verifier, or task.

**Model and evaluation set are controller-owned.** The fixed rollout model and
the frozen Harbor task set are stated in the controller prompt.
`meta_harness.py` chooses the trial count; the default search path uses 2 trials
per task.

**Key files to read:**

- `src/minisweagent/agents/default.py` - the main agent control flow.
- `src/minisweagent/config/mini.yaml` - the allowed agent prompt/config surface;
  locked runtime/model/budget fields must remain unchanged.
- `src/minisweagent/models/utils/` - permitted tool registry/parser/builder
  utilities, subject to the workspace policy's forbidden-file list.

You should implement in the controller-provided H0-derived workspace as a
starting point. **You can rewrite or override ANY method within the allowed
paths.** Never import from other candidate agents. Copy any code you want to
reuse.

Key extension surfaces:

- `DefaultAgent.execute_actions` - intercepts/executes parsed actions; use for
  submission or control-flow gates that apply to real rollouts.
- `src/minisweagent/config/mini.yaml` - the instance template and allowed agent
  configuration text; do not change locked runtime/model/budget fields.
- `models/utils/tool_registry.py` - fixed-provider tool schemas; changes must
  remain reachable from the real registry/parser/builder path.
- `models/utils/actions_toolcall.py` - tool-call action parsing/observation
  formatting used by the fixed LiteLLM provider boundary.

## CANDIDATE DESIGN

Each candidate is a **whole controller-provided workspace**, not a new Python
file or an `AgentHarness` subclass. The controller commits the edited workspace,
validates it, and freezes one candidate-specific snapshot before Harbor loads
it through the fixed import path
`adapters.harbor_mini_swe_agent:CandidateMiniSweAgent`.

### What you can and cannot modify

- **CAN**: edit `src/minisweagent/agents/**`,
  `src/minisweagent/config/mini.yaml`, and policy-permitted files under
  `src/minisweagent/models/utils/**`.
- **CANNOT**: modify provider/model classes, environments, runner, dependencies,
  tests, docs, git metadata, the frozen H0 source, runtime/evaluation locks,
  policy, suite/manifest, verifier, task, or a frozen candidate snapshot.
- `src/minisweagent/models/utils/retry.py` is fixed by policy even though it is
  beneath `models/utils/`.
- Tool-related changes are allowed, but they must be reachable from the real
  `DefaultAgent → fixed LiteLLM provider → registry/parser/builder` call path.

### Design principles

- Your primary goal is to improve the agent's performance (pass rate) on the frozen configured Harbor evaluation set.
- One mechanism per candidate. Each candidate tests exactly one hypothesis. If you're tempted to add "and also..." -- that's a second candidate.
- Mechanism-first. Identify a specific failure mode or hypothesis from trajectories, then design changes that target it. Never add changes speculatively.

## WORKFLOW

### Step 1: Analyze

1. Read state files named in the controller prompt:
   - `frontier.json` - current best agent per task and overall;
   - `evolution_summary.jsonl` - what's been tried, what worked/didn't, plus
     `rollout_metrics`;
   - validator reports and complete Harbor trial directories under the current
     run.
2. **Deep-read failed AND successful trajectories.** Most important step.
   Use the current run outputs under `evaluations/` and `proposer_sessions/` as
   the primary source of truth.
3. Read the fixed H0 source and any read-only frozen candidate snapshots when
   useful. Do not edit them.
4. Form one general-purpose, falsifiable mechanism. Use this return shape
   internally:

```text
STATE: <5-line summary: current scores, what's been tried, avg tokens/calls/cost>

HYPOTHESIS: "<falsifiable claim about what will improve scores>"
CANDIDATE: name=<snake_case>, changes="<specific changes>", prediction="<expected pass rate improvement AND expected token/call/turn impact>"
```

### Step 2: Implement

Implement exactly the Step 1 mechanism in the controller-provided current
workspace. Do not create a candidate subdirectory or edit a snapshot.

1. Change only policy-allowed paths in the current H0-derived workspace. The
   final implementation must remain loadable through the fixed Mini Harbor
   bridge.
2. Make targeted changes according to the candidate instructions.
3. Run only local static/unit checks. Do not contact Harbor, E2B, a model, or a
   benchmark task. Do not run a benchmark.
4. Leave the workspace uncommitted; the controller performs the authoritative
   validation and candidate commit/snapshot.

### Step 3: Write pending_eval.json

Write `pending_eval.json` to the path specified in the task prompt:

```json
{
  "iteration": <N>,
  "candidates": [
    {
      "name": "<name>",
      "hypothesis": "<falsifiable claim>",
      "changes": "<what was changed>",
      "expected_efficiency": "<expected token/model-call/wall-time impact>"
    }
  ]
}
```

Create exactly one candidate and use the iteration number supplied by the
controller. Always fill the descriptive fields: they are evidence for future
proposals, not a substitute for code validation.

Output: `CANDIDATES: <name1>`

## IMPORTANT NOTES

- Do not commit the candidate. The controller validates it and freezes the
  candidate-specific snapshot after checking the AHE-aligned policy.
- `DefaultAgent` and the fixed LiteLLM provider are the real rollout path; a
  tool change that is not reachable through that path has no evaluation effect.
- The Mini instance template is rendered with runtime task fields. Preserve its
  required template syntax and locked YAML fields.
- Never add a benchmark task name, task-specific answer, verifier/solution path,
  or split-specific conditional to candidate code, prompts or comments.
