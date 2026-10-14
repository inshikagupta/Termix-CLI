"""
Tool registry: maps KB rule IDs to deterministic tool handlers.

Structure
---------
TOOL_REGISTRY : dict[rule_id, callable]
    Key   → KB rule ``id`` string (e.g. "fs_create_file")
    Value → callable(args: dict) → ToolResult

Fallthrough
-----------
Rules NOT in the registry (git_*, npm_*, python_*, sys_*) fall through
to the shell renderer in tool_runner.py — their OS-specific command
template is filled with extracted placeholder values and passed to the
shell executor.

Extending
---------
To add a new tool:
  1. Write the handler in the appropriate module.
  2. Add one line here:  TOOL_REGISTRY["rule_id"] = lambda args: my_handler(...)
"""

from __future__ import annotations

from typing import Callable, Dict

from tools.types import ToolResult
from filesystem.file_agent import (
    tool_create_file,
    tool_create_folder,
    tool_remove_file,
    tool_remove_folder,
    tool_copy_file,
    tool_move_file,
    tool_show_file_contents,
    tool_find_files_by_name,
    tool_search_text_in_files,
)
from tools.app_launcher import tool_open_app


# Handler callable type: receives extracted args dict, returns ToolResult
HandlerFn = Callable[[Dict[str, str]], ToolResult]


TOOL_REGISTRY: Dict[str, HandlerFn] = {
    # ── Filesystem ─────────────────────────────────────────────────────────
    "fs_create_file": lambda args: tool_create_file(args.get("filename", "")),
    "fs_create_folder": lambda args: tool_create_folder(args.get("folder", "")),
    "fs_remove_file": lambda args: tool_remove_file(args.get("filename", "")),
    "fs_remove_folder": lambda args: tool_remove_folder(args.get("folder", "")),
    "fs_copy_file": lambda args: tool_copy_file(args.get("source", ""), args.get("destination", "")),
    "fs_move_file": lambda args: tool_move_file(args.get("source", ""), args.get("destination", "")),
    "fs_show_file_contents": lambda args: tool_show_file_contents(args.get("filename", "")),
    "fs_find_file_by_name": lambda args: tool_find_files_by_name(args.get("filename", "")),
    "fs_search_text_in_files": lambda args: tool_search_text_in_files(args.get("pattern", "")),

    # ── App launching ───────────────────────────────────────────────────────
    "open_app": lambda args: tool_open_app(args.get("app_name", "")),
    "open_app_in_current_dir": lambda args: tool_open_app(args.get("app_name", ""), in_current_dir=True),

    # ── Native OS app shortcuts (no placeholder) ───────────────────────────
    "app_open_terminal": None,        # type: ignore[assignment]
    "app_open_file_manager": None,    # type: ignore[assignment]
    "app_open_calculator": None,      # type: ignore[assignment]
    "fs_list_files": None,            # type: ignore[assignment]
    "fs_current_directory": None,     # type: ignore[assignment]

    # ── Shell passthrough: cd intercepted by server.py _handle_cd() ────────
    "fs_change_directory": None,      # type: ignore[assignment]

    # ── Git shell passthroughs ─────────────────────────────────────────────
    "git_status": None,               # type: ignore[assignment]
    "git_add_all": None,              # type: ignore[assignment]
    "git_commit": None,               # type: ignore[assignment]
    "git_add_commit": None,           # type: ignore[assignment]
    "git_branch_list": None,          # type: ignore[assignment]
    "git_checkout_branch": None,      # type: ignore[assignment]
    "git_branch_create": None,        # type: ignore[assignment]
    "git_pull": None,                 # type: ignore[assignment]
    "git_push": None,                 # type: ignore[assignment]
    "git_diff": None,                 # type: ignore[assignment]
    "git_log": None,                  # type: ignore[assignment]
    "git_stash": None,                # type: ignore[assignment]
    "git_stash_pop": None,            # type: ignore[assignment]
    "git_init": None,                 # type: ignore[assignment]
    "git_clone": None,                # type: ignore[assignment]
    "git_reset_last": None,           # type: ignore[assignment]
    "git_remote": None,               # type: ignore[assignment]

    # ── Port / process shell passthroughs ──────────────────────────────────
    "sys_show_processes": None,       # type: ignore[assignment]
    "sys_kill_process": None,         # type: ignore[assignment]
    "sys_ping_host": None,            # type: ignore[assignment]
    "sys_kill_port": None,            # type: ignore[assignment]
    "sys_check_port": None,           # type: ignore[assignment]
    "sys_kill_by_name": None,         # type: ignore[assignment]
    "sys_show_ports": None,           # type: ignore[assignment]

    # ── npm shell passthroughs ─────────────────────────────────────────────
    "npm_install": None,              # type: ignore[assignment]
    "npm_install_package": None,      # type: ignore[assignment]
    "npm_install_dev": None,          # type: ignore[assignment]
    "npm_uninstall": None,            # type: ignore[assignment]
    "npm_run_dev": None,              # type: ignore[assignment]
    "npm_run_build": None,            # type: ignore[assignment]
    "npm_run_script": None,           # type: ignore[assignment]
    "npm_list": None,                 # type: ignore[assignment]

    # ── Python / pip shell passthroughs ────────────────────────────────────
    "python_create_venv": None,       # type: ignore[assignment]
    "python_activate_venv": None,     # type: ignore[assignment]
    "python_pip_install": None,       # type: ignore[assignment]
    "python_run_file": None,          # type: ignore[assignment]
    "pip_uninstall": None,            # type: ignore[assignment]
    "pip_install_requirements": None, # type: ignore[assignment]
    "pip_freeze": None,               # type: ignore[assignment]
    "pip_list": None,                 # type: ignore[assignment]
}
