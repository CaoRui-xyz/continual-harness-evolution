#!/usr/bin/env bash
# Turn an unpacked official Harbor 0.8.0 source tree into the Harbor tree used
# by HarnessFix (https://github.com/HarnessFix/HarnessFix, commit 9167a0b,
# task_agent/terminal_bench_agent/harbor).
#
# Usage: bash apply_patch.sh <path-to-harbor-0.8.0-source>
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:?usage: apply_patch.sh <path-to-harbor-0.8.0-source>}"
TARGET="$(cd "$TARGET" && pwd)"

if [[ ! -f "$TARGET/src/harbor/agents/installed/mini_swe_agent.py" ]]; then
    echo "error: $TARGET does not look like a Harbor source tree" >&2
    exit 1
fi
if grep -q "host_bridge" "$TARGET/src/harbor/agents/installed/mini_swe_agent.py"; then
    echo "Harbor at $TARGET is already patched; nothing to do."
    exit 0
fi

patch -p1 -d "$TARGET" < "$HERE/harnessfix-harbor-0.8.0.patch"

# HarnessFix removes the agents it does not register in agents/factory.py.
INSTALLED="$TARGET/src/harbor/agents/installed"
rm -rf \
    "$INSTALLED/aider.py" \
    "$INSTALLED/claude_code.py" \
    "$INSTALLED/cline" \
    "$INSTALLED/codex.py" \
    "$INSTALLED/copilot_cli.py" \
    "$INSTALLED/cursor_cli.py" \
    "$INSTALLED/devin.py" \
    "$INSTALLED/gemini_cli.py" \
    "$INSTALLED/goose.py" \
    "$INSTALLED/hermes.py" \
    "$INSTALLED/kimi_cli.py" \
    "$INSTALLED/nemo_agent.py" \
    "$INSTALLED/nemo_agent_run_wrapper.py" \
    "$INSTALLED/opencode.py" \
    "$INSTALLED/openhands.py" \
    "$INSTALLED/openhands_sdk.py" \
    "$INSTALLED/openhands_sdk_runner.py" \
    "$INSTALLED/pi.py" \
    "$INSTALLED/qwen_code.py" \
    "$INSTALLED/rovodev_cli.py" \
    "$INSTALLED/swe_agent.py" \
    "$INSTALLED/trae_agent.py"

echo "Patched Harbor at $TARGET"
