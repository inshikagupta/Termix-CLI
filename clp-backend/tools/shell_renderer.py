"""
Shell renderer: fills OS-specific command templates with extracted placeholder values.

Used for rules that stay as shell commands (git, npm, python, sys, ping, etc.)
— i.e. any rule NOT handled by a Python-native tool in file_agent.py or app_launcher.py.

Design rules:
  - ONLY render after all required placeholders are present (no partial fills).
  - Quote values before substitution to prevent injection.
  - Return None if any <placeholder> remains unfilled after substitution.
"""

from __future__ import annotations

import re
import shlex
from typing import Any, Dict, List, Optional

from utils.os_detector import detect_os


# ── Helpers ──────────────────────────────────────────────────────────────────

def _os_key() -> str:
    os_name = detect_os()
    if os_name == "mac":
        return "macos"
    if os_name == "windows":
        return "windows"
    return "linux"


def _quote_value(value: str, os_key: str) -> str:
    if not value:
        return value
    # Already quoted
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value
    # Tilde paths: never quote, or shell won't expand ~
    if value == "~" or value.startswith("~/"):
        return value
    # Safe bare value
    if re.match(r'^[\w./-]+$', value):
        return value
    # Needs quoting
    if os_key == "windows":
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return shlex.quote(value)


# Detect unfilled placeholders
_UNFILLED_RE = re.compile(r'<\w+>')


# ── Public API ────────────────────────────────────────────────────────────────

def render_command(
    rule: Dict[str, Any],
    args: Dict[str, str],
) -> Optional[str]:
    """
    Fill the OS-specific command template for *rule* with *args*.

    Parameters
    ----------
    rule : dict
        A KB rule with a ``commands`` map (e.g. {"linux": "...", "macos": "...", "windows": "..."}).
    args : dict
        Extracted placeholder values, e.g. {"filename": "notes.txt"}.

    Returns
    -------
    str or None
        The rendered shell command string, or None if:
        - The commands map is missing / has no entry for the current OS.
        - Any <placeholder> remains unfilled after substitution.
    """
    commands_map = rule.get("commands") if isinstance(rule, dict) else {}
    if not isinstance(commands_map, dict):
        return None

    os_key = _os_key()
    template = commands_map.get(os_key) or commands_map.get("linux")
    if not isinstance(template, str) or not template.strip():
        return None

    rendered = _fill_template(template, args, os_key)

    # Reject if any placeholder is still present
    if _UNFILLED_RE.search(rendered):
        return None

    return rendered


def _fill_template(template: str, args: Dict[str, str], os_key: str) -> str:
    """Replace all <name> tokens in template with quoted values from args."""
    result = template
    for name, value in args.items():
        quoted = _quote_value(value, os_key)
        result = result.replace(f"<{name}>", quoted)
    return result


def detect_remaining_placeholders(rendered: str) -> List[str]:
    """Return placeholder names still present in a rendered command."""
    return _UNFILLED_RE.findall(rendered)