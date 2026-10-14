"""
Security validation stub.

Intended to enforce deny-lists and command policy gates before execution.
"""

from __future__ import annotations

import re



_ALLOWLIST_PREFIXES = {
    "ls",
    "pwd",
    "echo",
    "cat",
    "mkdir",
    "touch",
    "npm",
    "pip",
    "python",
    "node",
}

_SAFE_DELETE_PREFIXES = {
    "./",
    "tmp/",
    "/tmp/",
    "/var/tmp/",
}

# Fork bomb pattern: creates uncontrolled process explosion.
_FORK_BOMB_PATTERN = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE)

# Dangerous / destructive command patterns.
_RM_ROOT_PATTERN = re.compile(r"\brm\b.*\s-\w*[rf]\w*\b.*\s/\s*(?:$|[;&|])", re.IGNORECASE)
_RM_RECURSIVE_PATTERN = re.compile(r"\brm\b\s+-(?:[^\s]*r[^\s]*|[^\s]*R[^\s]*)\s+(.+)$", re.IGNORECASE)
_MKFS_PATTERN = re.compile(r"\bmkfs(?:\.\w+)?\b", re.IGNORECASE)
_SHUTDOWN_PATTERN = re.compile(r"\bshutdown\b", re.IGNORECASE)
_REBOOT_PATTERN = re.compile(r"\breboot\b", re.IGNORECASE)
_DD_PATTERN = re.compile(r"\bdd\b", re.IGNORECASE)

# Overly permissive root permission changes are high-risk.
_CHMOD_777_ROOT_PATTERN = re.compile(r"\bchmod\b\s+777\s+/\s*(?:$|[;&|])", re.IGNORECASE)

# chown on / can recursively reassign system ownership.
_CHOWN_ROOT_PATTERN = re.compile(r"\bchown\b(?:\s+-\w+)*\s+[^;&|]*\s/\s*(?:$|[;&|])", re.IGNORECASE)

# Disk overwrite redirection can destroy boot/data disks.
_DISK_OVERWRITE_PATTERN = re.compile(r">\s*/dev/(?:sd[a-z]\d*|nvme\d+n\d+p?\d*|vd[a-z]\d*)\b", re.IGNORECASE)

# kill -9 -1 sends SIGKILL to almost all permitted processes.
_KILL_ALL_PATTERN = re.compile(r"\bkill\b\s+-9\s+-1\b", re.IGNORECASE)
_UNRESOLVED_PLACEHOLDER_PATTERN = re.compile(r"<[A-Za-z_]\w*>")


def _is_allowlisted(command: str) -> bool:
    """
    Basic allowlist signal for common benign commands.
    This is NOT restrictive; non-allowlisted commands can still pass if safe.
    """
    first = command.split(" ", 1)[0].strip().lower()
    return first in _ALLOWLIST_PREFIXES


def contains_unresolved_placeholders(command: str) -> bool:
    """
    Return True if a command still contains template placeholders like <folder>.
    Such commands must never reach shell execution.
    """
    return bool(_UNRESOLVED_PLACEHOLDER_PATTERN.search(command or ""))


def _is_recursive_delete_outside_safe_dirs(collapsed_cmd: str) -> bool:
    """
    Block recursive delete targets unless clearly in a small set of safe prefixes.
    """
    match = _RM_RECURSIVE_PATTERN.search(collapsed_cmd)
    if not match:
        return False

    target = match.group(1).strip().strip("\"'")
    if not target:
        return True

    # Hard-block obvious dangerous targets.
    if target in {"/", "~", ".", ".."}:
        return True
    if target.startswith("/"):
        return not any(target.startswith(prefix) for prefix in _SAFE_DELETE_PREFIXES)
    if target.startswith("./"):
        return False
    if target.startswith("tmp/"):
        return False
    return True


def validate_commands(commands: list[str]) -> bool:
    """
    Return False if any command is unsafe; otherwise True.

    Blocks known-dangerous commands/patterns:
    - rm -rf /
    - mkfs (any variant)
    - shutdown
    - reboot
    - dd
    - shell fork bomb :(){:|:&};:
    """
    for raw in commands:
        original = (raw or "").strip()
        if not original:
            continue

        if contains_unresolved_placeholders(original):
            return False

        collapsed = re.sub(r"\s+", " ", original)

        # Fork bomb (allow for whitespace variation).
        if _FORK_BOMB_PATTERN.search(original):
            return False

        # rm -rf / (also catches -fr)
        if _RM_ROOT_PATTERN.search(collapsed):
            return False
        lowered = collapsed.lower()
        if lowered.startswith("rm -rf /") or lowered.startswith("rm -fr /"):
            return False

        # Recursive deletes should be constrained to explicitly safe paths.
        if _is_recursive_delete_outside_safe_dirs(collapsed):
            return False

        # Block destructive disk/system commands.
        if _MKFS_PATTERN.search(collapsed):
            return False
        if _SHUTDOWN_PATTERN.search(collapsed):
            return False
        if _REBOOT_PATTERN.search(collapsed):
            return False
        if _DD_PATTERN.search(collapsed):
            return False

        # chmod 777 / opens full-system write permissions.
        if _CHMOD_777_ROOT_PATTERN.search(collapsed):
            return False

        # chown on / can recursively compromise ownership integrity.
        if _CHOWN_ROOT_PATTERN.search(collapsed):
            return False

        # Redirecting output into block devices can corrupt disks.
        if _DISK_OVERWRITE_PATTERN.search(collapsed):
            return False

        # kill -9 -1 can terminate most processes on the host.
        if _KILL_ALL_PATTERN.search(collapsed):
            return False

        # Basic allowlist logic (non-strict): recognized safe command prefixes pass this check.
        # Non-allowlisted commands are still allowed unless deny-list patterns matched above.
        _ = _is_allowlisted(lowered)

    return True

