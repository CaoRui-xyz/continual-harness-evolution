"""Run candidate model utilities under a thread-scoped host-side audit boundary."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Any


_AUDIT_STATE = threading.local()
_BLOCKED_AUDIT_EVENTS = (
    "open",
    "os.chdir",
    "os.chmod",
    "os.chown",
    "os.link",
    "os.listdir",
    "os.mkdir",
    "os.putenv",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.scandir",
    "os.symlink",
    "os.system",
    "os.truncate",
    "os.unsetenv",
    "shutil.",
    "socket.",
    "subprocess.Popen",
    "tempfile.",
)


def _candidate_utility_audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if not getattr(_AUDIT_STATE, "depth", 0):
        return
    if any(event == prefix or event.startswith(prefix) for prefix in _BLOCKED_AUDIT_EVENTS):
        raise PermissionError(f"candidate Mini utility attempted blocked host audit event: {event}")


sys.addaudithook(_candidate_utility_audit_hook)


def _candidate_profile_hook(frame: Any, event: str, arg: Any) -> None:
    if event != "call" or not getattr(_AUDIT_STATE, "depth", 0):
        return
    module_name = str(frame.f_globals.get("__name__", ""))
    if module_name == "minisweagent.models.host_guard":
        return
    if module_name.startswith("minisweagent.models.") and not module_name.startswith("minisweagent.models.utils."):
        raise PermissionError(f"candidate Mini utility attempted fixed model call: {module_name}.{frame.f_code.co_name}")
    if module_name.startswith(("adapters.", "harbor.")):
        raise PermissionError(f"candidate Mini utility attempted host infrastructure call: {module_name}.{frame.f_code.co_name}")


def guarded_candidate_utility(function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    previous_depth = int(getattr(_AUDIT_STATE, "depth", 0) or 0)
    previous_profile = sys.getprofile()
    _AUDIT_STATE.depth = previous_depth + 1
    sys.setprofile(_candidate_profile_hook)
    try:
        return function(*args, **kwargs)
    finally:
        sys.setprofile(previous_profile)
        _AUDIT_STATE.depth = previous_depth


__all__ = ["guarded_candidate_utility"]
