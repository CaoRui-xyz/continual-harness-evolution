"""Trusted controller helpers; candidate Mini code never lives in this package."""

from pathlib import Path
import sys

_AHE_ROOT = Path(__file__).resolve().parents[4] / "agentic-harness-engineering"
if str(_AHE_ROOT) not in sys.path:
    sys.path.insert(0, str(_AHE_ROOT))

