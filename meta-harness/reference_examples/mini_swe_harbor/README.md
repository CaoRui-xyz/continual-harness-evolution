# Mini-SWE-Agent Harbor reference experiment

This is a new Meta-Harness reference experiment. It preserves the original
Terminal-Bench 2 method—baseline, proposal, validation/smoke, full benchmark,
per-task and overall frontier update, then optional five-trial full evaluation
of the non-baseline winner—while evolving a Mini-SWE-Agent workspace.

The controller deliberately does **not** inherit AHE's evolution loop.  AHE is
used only for the Mini-SWE-Agent validator and the Harbor/E2B runtime adapters;
candidate generation, fixed-H0 independent variants, frontier selection and
the optional five-trial winner evaluation are Meta-Harness semantics.

The evaluator intentionally uses the same Mini host bridge, evaluation lock,
workspace policy, Harbor 0.8 CLI arguments, and `AheE2BEnvironment` as
`agentic-harness-engineering`. Harbor is the required evaluator. The old
TB2 Harbor 0.18 environment remains untouched and isolated.

## Important contracts

- The SWE/SWT/Feature search suites read the shared `datasets/<name>/task_names.txt`
  and execute `datasets/<name>/tasks/`. Other benchmarks may use registry manifests.
- `--n-attempts` is the sole rollout count. The runtime lock currently permits
  up to 20 E2B sandboxes; individual runs may choose a lower concurrency.
- Candidate iterations use the same single-task local Harbor smoke gate as the
  source TB2 reference before a full search evaluation. Smoke requires a
  complete terminal trial, not a solved task; only search-suite matrices can
  update the frontier.
- Each iteration starts from the same immutable H0 workspace. A frontier winner
  is never silently made the next Git parent. The proposer may nevertheless
  inspect a read-only prior snapshot and deliberately copy an allowed mechanism,
  just as the original TB2 proposer may copy an existing agent file. This keeps
  the source experiment's search freedom without hidden hill-climbing state.
- The frontier has the original two views: best candidate for each task and
  best candidate by flat aggregate score. Ties keep the earlier frontier entry.
- A score exists only when every declared `task × attempt` has one terminal
  Harbor artifact. Missing, duplicate, cancelled, malformed, or unexpected
  trials make the evaluation incomplete and cannot update the frontier.
- Candidate source is validated with AHE's authoritative Mini validator before
  it is copied to a frozen evaluation snapshot. The proposer never edits the
  snapshot used by Harbor.
- The outer proposer uses the provider-neutral `proposer_backend.py` LiteLLM
  tool loop. It defaults to the evaluation-lock model (`deepseek/deepseek-v4-flash`)
  and accepts `--proposer-model`, `--proposer-api-base`, and
  `--proposer-api-key-env` for other LiteLLM/OpenAI-compatible providers.
- Model/provider/endpoint/budget/environment fields are owned by
  `configs/evaluation_lock.yaml`, not by the evolved workspace.
- Runs created by an older controller schema cannot be resumed. The current
  schema records the H0 tree, candidate-config path and the hashes of all
  external Mini/Harbor/E2B runtime inputs, so start a new `--run-name` after
  upgrading rather than mixing old records with the new frozen-input contract.

## Bring-up

```bash
cd meta-harness/reference_examples/mini_swe_harbor
uv sync
uv run pytest -q
uv run python meta_harness.py --offline-evolution-test
```

For a real Harbor/E2B run, set `AHE_ROOT`, `META_HARBOR_RUNNER_PYTHON`,
`META_HARBOR_SOURCE_DIR`, `MINI_SWE_SEED_DIR`, `E2B_API_KEY`, and `LLM_API_KEY`.
Then begin with an explicitly prepared one-task local suite; do not start the
50-task configurations before that smoke succeeds.

The committed one-task smoke config is
`configs/evolution/swebench-smoke.yaml`; it refers only to the
first task in the bundled SWE-bench selection and uses one E2B sandbox. It does not run
without credentials and should never be reported as a SWE-bench score.

Like the original TB2 experiment, the outer CLI—not an experiment YAML field—
controls the number of search iterations and attempts. For this bring-up run,
be explicit:

```bash
uv run python meta_harness.py \
  --config configs/evolution/swebench-smoke.yaml --iterations 1 --trials 1
```

The default proposer key is the model-specific key declared by the evaluation
lock (`DEEPSEEK_API_KEY` for the shipped DeepSeek model). To use another
LiteLLM model, pass its model id, endpoint, and key variable explicitly:

```bash
uv run python meta_harness.py \
  --proposer-model openai/my-coding-model \
  --proposer-api-base https://example.invalid/v1 \
  --proposer-api-key-env OPENAI_API_KEY
```

The three search datasets use `task_names_file` and a local `source.path`.
Their task lists are shared with AHE under the monorepo `datasets/` directory.
SWT uses the paraphrased 50 tasks; FeatureBench uses only the selected 30 tasks.

Prepare E2B templates with the exact AHE E2B adapter used in rollout (never
above the runtime-lock capacity):

```bash
uv run python scripts/prepare_e2b_templates.py \
  --config configs/evolution/swebench.yaml \
  --template-concurrency 4
```

## LiveCodeBench release_v6: all 100 tasks

`configs/evolution/livecodebench-release-v6-all100-k2.yaml` freezes the 100
tasks in Harbor's `livecodebench@6.0` registry at commit
`cfad8d97c75fb9e28c7b6b74b7fadb23dae57ab6`. Its search contract is 100 tasks ×
2 rollouts, four concurrent E2B sandboxes, and the source Meta-Harness default
of five evolution iterations. Thus a normal five-iteration run evaluates H0
plus five candidates (at most 1,200 search rollouts), with one additional
single-task smoke rollout for each candidate that passes validation.

First prepare the task cache and all task-specific E2B templates. This uses
the same `AheE2BEnvironment` as rollout. It does not run the Mini agent or a
verifier:

```bash
cd meta-harness/reference_examples/mini_swe_harbor
./scripts/prepare_livecodebench_e2b_templates.sh
```

The cache bootstrap downloads the complete Git tree for that exact commit,
then downloads only the unique Git blobs used by the selected 100 task
directories. It verifies every blob SHA, restores executable bits, and writes
to Harbor's normal cache under `~/.cache/harbor/tasks`. Later Harbor commands
still use their canonical registry `GitTaskId` and fixed commit. If the
official GitHub API endpoint is unavailable, choose a trusted API mirror
explicitly. The default raw transfer fallback is `ghproxy.net`, but every
file obtained through it is verified against the fixed Git tree's blob SHA;
the report records the canonical source and both endpoints:

```bash
META_LIVECODEBENCH_GITHUB_API_BASE='https://<your-api-mirror>' \
META_LIVECODEBENCH_GITHUB_RAW_BASE='https://<your-raw-mirror>/laude-institute/harbor-datasets/cfad8d97c75fb9e28c7b6b74b7fadb23dae57ab6' \
  ./scripts/prepare_livecodebench_e2b_templates.sh
```

The first successful API-tree request is reusable. For example, if you saved
that full response as `/path/to/tree.json`, use the trusted fixed tree metadata
without making another anonymous API request; all downloaded blobs are still
verified against its SHA-1 values:

```bash
META_LIVECODEBENCH_LOCAL_TREE_CACHE=/path/to/tree.json \
  ./scripts/prepare_livecodebench_e2b_templates.sh
```

If GitHub's anonymous API rate limit is exhausted, download the official
fixed-commit codeload archive once, then use it as an offline cache source.
The bootstrap only accepts an archive whose top-level directory includes the
pinned full commit, extracts only these 100 task directories, and records the
archive SHA-256:

```bash
curl --fail --location --output ./harbor-datasets-cfad8d97.tar.gz \
  https://codeload.github.com/laude-institute/harbor-datasets/tar.gz/cfad8d97c75fb9e28c7b6b74b7fadb23dae57ab6
META_LIVECODEBENCH_LOCAL_ARCHIVE=./harbor-datasets-cfad8d97.tar.gz \
  ./scripts/prepare_livecodebench_e2b_templates.sh
```

After all 100 template aliases are reported as ready, launch the five-round
source-style search:

```bash
./scripts/run_livecodebench_all100_k2.sh
```

The script preserves the original search `k=2` through `--trials 2`; it does
not use the AHE `rollouts_per_task` setting. It passes the controller's
`--iterations 5` explicitly because, as in original Meta-Harness, iterations
are an outer CLI setting rather than a YAML field. A later optional final pass
can be requested with `--full-eval`; it evaluates the non-baseline winner with
the original fixed five rollouts per task and is reported separately.

The no-credential preflight is available separately:

```bash
uv run python scripts/prepare_e2b_templates.py \
  --config configs/evolution/swebench-smoke.yaml --check-only
```

The optional final phase has the same purpose and default trial count as TB2:
it evaluates the frozen, non-baseline overall winner on the same configured
search suite with five attempts. It is reported separately and never rewrites
the search frontier:

```bash
uv run python meta_harness.py --config <search-config> --run-name <run> \
  --full-eval
```
