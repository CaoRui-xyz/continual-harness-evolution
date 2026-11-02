"""Reuse the original Meta-Harness TB2 proposer logging implementation.

The Mini experiment intentionally keeps the outer proposer integration used by
the source repository.  Loading by file path avoids changing Python package
paths or the legacy reference experiment's Harbor 0.18 environment.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


_SOURCE = Path(__file__).resolve().parents[1] / "terminal_bench_2" / "claude_wrapper.py"
_SPEC = importlib.util.spec_from_file_location("_meta_harness_tb2_claude_wrapper", _SOURCE)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load original Meta-Harness claude wrapper: {_SOURCE}")
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

run = _MODULE.run
SessionResult = _MODULE.SessionResult

