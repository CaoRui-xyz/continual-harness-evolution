#!/usr/bin/env bash
set -euo pipefail

# Pass the actual experiment and configuration through to the portable launcher.
if [[ $# -eq 0 ]]; then
    echo "用法: $0 --experiment EXPERIMENT --start-iteration N [--skip-eval] CONFIG"
    exit 2
fi
exec "$(dirname "$0")/evolve.sh" "$@"
