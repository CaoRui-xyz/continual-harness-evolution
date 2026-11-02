#!/usr/bin/env bash
# Meta-Harness evolution with configs/evolution/featurebench-to-swebench.yaml.
# Usage: run_featurebench_to_swebench.sh [run-name]   (default run name: featurebench-to-swebench; rerun with the same name to resume)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$ROOT/../../.." && pwd)"
RUNNER="${META_HARBOR_RUNNER_PYTHON:-${HARNESS_RUNTIME_DIR:-$REPO_ROOT/../runtime}/harbor-venv/bin/python}"
RUN_NAME="${1:-featurebench-to-swebench}"

exec "$RUNNER" "$ROOT/meta_harness.py" \
  --config "$ROOT/configs/evolution/featurebench-to-swebench.yaml" \
  --run-name "$RUN_NAME" \
  --iterations 5 \
  --trials 2 \
  --concurrent 20 \
  --propose-timeout 3600 \
  --proposer-model deepseek/deepseek-v4-flash \
  --proposer-max-steps 200
