"""
Filesystem tool module — canonical Python-backed handlers for all fs_* KB rules.

Design principles:
  - Use Python stdlib (pathlib, shutil, fnmatch, glob) instead of shelling out
    for all filesystem ops.  No subprocess for create/copy/move/read/find.
  - Shell commands are rendered alongside for user display ("rendered_commands").
  - Destructive operations (remove_file, remove_folder) set requires_confirmation=True.
  - Every function returns a ToolResult with a consistent contract.

This module is the ONLY place for filesystem Python-native logic.
utils/file_ops.py keeps only parsing helpers (extract_filename, etc.).
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.types import ToolResult
from utils.os_detector import detect_os



# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_path(raw: str, base_dir: Optional[str] = None) -> Path:
    """
    Resolve a user-supplied path safely.

    Rules:
    - Absolute paths  (/Users/shubham/Desktop/file.txt)  → used as-is
    - Home-relative   (~/Desktop/file.txt)                → expanded via expanduser()
    - Bare relative   (notes.txt, myfolder/sub)           → anchored to base_dir (session cwd)
                                                            or server cwd if base_dir is None

    Traversal guard: blocks paths that try to escape via ../../ tricks on relative paths.
    """
    p = Path(raw.strip())

    # Absolute path — use directly
    if p.is_absolute():
        return p.resolve()

    # Home-relative path (~/...)
    if raw.strip().startswith("~"):
        return p.expanduser().resolve()

    # Bare relative path — anchor to session cwd (or server cwd as fallback)
    anchor = Path(os.path.expanduser(base_dir)) if base_dir else Path.cwd()
    resolved = (anchor / p).resolve()
    try:
        resolved.relative_to(anchor.resolve())
    except ValueError:
        raise ValueError(
            f"Path '{raw}' escapes the working directory. "
            "Use an absolute path (e.g. ~/Desktop/file.txt) instead."
        )
    return resolved


def _os_key() -> str:
    os_name = detect_os()
    if os_name == "mac":
        return "macos"
    if os_name == "windows":
        return "windows"
    return "linux"


# ── Tool handlers ─────────────────────────────────────────────────────────────


def tool_create_file(filename: str) -> ToolResult:
    """
    Create a new empty file using Python pathlib.
    Renders the equivalent shell command for display.
    """
    if not filename or not filename.strip():
        return ToolResult(
            tool_name="create_file",
            missing_placeholders=["filename"],
            safe_to_execute=False,
        )

    name = filename.strip()
    os_key = _os_key()
    shell_cmd = f'touch "{name}"' if os_key != "windows" else f'type nul > "{name}"'

    try:
        p = _safe_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(exist_ok=True)
        return ToolResult(
            tool_name="create_file",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            output=f"Created file: {p}",
            executed=True,
            safe_to_execute=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="create_file",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=str(exc),
        )


def tool_create_folder(folder: str) -> ToolResult:
    """
    Create a new directory (with parents) using Python pathlib.
    """
    if not folder or not folder.strip():
        return ToolResult(
            tool_name="create_folder",
            missing_placeholders=["folder"],
            safe_to_execute=False,
        )

    name = folder.strip()
    os_key = _os_key()
    shell_cmd = f'mkdir -p "{name}"' if os_key != "windows" else f'mkdir "{name}"'

    try:
        p = _safe_path(name)
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult(
            tool_name="create_folder",
            arguments={"folder": name},
            rendered_commands=[shell_cmd],
            output=f"Created directory: {p}",
            executed=True,
            safe_to_execute=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="create_folder",
            arguments={"folder": name},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=str(exc),
        )


def tool_remove_file(filename: str) -> ToolResult:
    """
    Delete a single file.  ALWAYS requires_confirmation=True.
    Does NOT execute immediately — caller must confirm then call execute().
    """
    if not filename or not filename.strip():
        return ToolResult(
            tool_name="remove_file",
            missing_placeholders=["filename"],
            safe_to_execute=False,
        )

    name = filename.strip()
    os_key = _os_key()
    shell_cmd = f'rm "{name}"' if os_key != "windows" else f'del "{name}"'

    # Does the file actually exist?  Warn if not.
    p = _safe_path(name)
    if not p.exists():
        return ToolResult(
            tool_name="remove_file",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            requires_confirmation=True,
            safe_to_execute=False,
            error=f"File not found: {name}",
        )

    return ToolResult(
        tool_name="remove_file",
        arguments={"filename": name},
        rendered_commands=[shell_cmd],
        requires_confirmation=True,
        safe_to_execute=True,
        # executed=False — caller confirms, then calls _execute_remove_file()
    )


def execute_remove_file(filename: str) -> ToolResult:
    """Execute file removal after user confirmation."""
    name = filename.strip()
    os_key = _os_key()
    shell_cmd = f'rm "{name}"' if os_key != "windows" else f'del "{name}"'
    try:
        _safe_path(name).unlink(missing_ok=True)
        return ToolResult(
            tool_name="remove_file",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            requires_confirmation=True,
            safe_to_execute=True,
            output=f"Deleted file: {name}",
            executed=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="remove_file",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            requires_confirmation=True,
            safe_to_execute=True,
            error=str(exc),
        )


def tool_remove_folder(folder: str) -> ToolResult:
    """
    Recursively delete a directory.  ALWAYS requires_confirmation=True.
    Does NOT execute immediately — caller must confirm then call execute().
    """
    if not folder or not folder.strip():
        return ToolResult(
            tool_name="remove_folder",
            missing_placeholders=["folder"],
            safe_to_execute=False,
        )

    name = folder.strip()
    os_key = _os_key()
    shell_cmd = (
        f'rm -rf "./{name}"' if os_key != "windows" else f'rmdir /s /q "./{name}"'
    )

    p = _safe_path(name)
    if not p.exists():
        return ToolResult(
            tool_name="remove_folder",
            arguments={"folder": name},
            rendered_commands=[shell_cmd],
            requires_confirmation=True,
            safe_to_execute=False,
            error=f"Directory not found: {name}",
        )

    return ToolResult(
        tool_name="remove_folder",
        arguments={"folder": name},
        rendered_commands=[shell_cmd],
        requires_confirmation=True,
        safe_to_execute=True,
    )


def execute_remove_folder(folder: str) -> ToolResult:
    """Execute folder removal after user confirmation."""
    name = folder.strip()
    os_key = _os_key()
    shell_cmd = (
        f'rm -rf "./{name}"' if os_key != "windows" else f'rmdir /s /q "./{name}"'
    )
    try:
        shutil.rmtree(_safe_path(name))
        return ToolResult(
            tool_name="remove_folder",
            arguments={"folder": name},
            rendered_commands=[shell_cmd],
            requires_confirmation=True,
            safe_to_execute=True,
            output=f"Deleted directory: {name}",
            executed=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="remove_folder",
            arguments={"folder": name},
            rendered_commands=[shell_cmd],
            requires_confirmation=True,
            safe_to_execute=True,
            error=str(exc),
        )


def tool_copy_file(source: str, destination: str) -> ToolResult:
    """Copy a file using shutil.copy2 (preserves metadata)."""
    missing = [s for s, v in [("source", source), ("destination", destination)] if not v.strip()]
    if missing:
        return ToolResult(tool_name="copy_file", missing_placeholders=missing, safe_to_execute=False)

    src, dst = source.strip(), destination.strip()
    os_key = _os_key()
    shell_cmd = f'cp "{src}" "{dst}"' if os_key != "windows" else f'copy "{src}" "{dst}"'

    try:
        src_path = _safe_path(src)
        dst_path = _safe_path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
        return ToolResult(
            tool_name="copy_file",
            arguments={"source": src, "destination": dst},
            rendered_commands=[shell_cmd],
            output=f"Copied {src} → {dst}",
            executed=True,
            safe_to_execute=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="copy_file",
            arguments={"source": src, "destination": dst},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=str(exc),
        )


def tool_move_file(source: str, destination: str) -> ToolResult:
    """Move or rename a file using shutil.move."""
    missing = [s for s, v in [("source", source), ("destination", destination)] if not v.strip()]
    if missing:
        return ToolResult(tool_name="move_file", missing_placeholders=missing, safe_to_execute=False)

    src, dst = source.strip(), destination.strip()
    os_key = _os_key()
    shell_cmd = f'mv "{src}" "{dst}"' if os_key != "windows" else f'move "{src}" "{dst}"'

    try:
        dst_path = _safe_path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(_safe_path(src)), str(dst_path))
        return ToolResult(
            tool_name="move_file",
            arguments={"source": src, "destination": dst},
            rendered_commands=[shell_cmd],
            output=f"Moved {src} → {dst}",
            executed=True,
            safe_to_execute=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="move_file",
            arguments={"source": src, "destination": dst},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=str(exc),
        )


def tool_show_file_contents(filename: str) -> ToolResult:
    """Read and return file contents directly via Python."""
    if not filename or not filename.strip():
        return ToolResult(tool_name="show_file_contents", missing_placeholders=["filename"], safe_to_execute=False)

    name = filename.strip()
    os_key = _os_key()
    shell_cmd = f'cat "{name}"' if os_key != "windows" else f'type "{name}"'

    try:
        p = _safe_path(name)
        content = p.read_text(encoding="utf-8", errors="replace")
        return ToolResult(
            tool_name="show_file_contents",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            output=content,
            executed=True,
            safe_to_execute=True,
        )
    except FileNotFoundError:
        return ToolResult(
            tool_name="show_file_contents",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=f"File not found: {name}",
        )
    except Exception as exc:
        return ToolResult(
            tool_name="show_file_contents",
            arguments={"filename": name},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=str(exc),
        )


def tool_find_files_by_name(filename: str) -> ToolResult:
    """Find files matching a name pattern under the current directory (Python glob)."""
    if not filename or not filename.strip():
        return ToolResult(tool_name="find_files_by_name", missing_placeholders=["filename"], safe_to_execute=False)

    pattern = filename.strip()
    os_key = _os_key()
    shell_cmd = f'find . -name "{pattern}"' if os_key != "windows" else f'dir /s /b "{pattern}"'

    try:
        matches = list(Path(".").rglob(pattern))
        output = "\n".join(str(m) for m in matches) if matches else f"No files found matching: {pattern}"
        return ToolResult(
            tool_name="find_files_by_name",
            arguments={"filename": pattern},
            rendered_commands=[shell_cmd],
            output=output,
            executed=True,
            safe_to_execute=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="find_files_by_name",
            arguments={"filename": pattern},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=str(exc),
        )


def tool_search_text_in_files(pattern: str) -> ToolResult:
    """
    Recursively search for a text pattern in all files under cwd (Python re).
    Returns matching file:line results.
    """
    if not pattern or not pattern.strip():
        return ToolResult(tool_name="search_text_in_files", missing_placeholders=["pattern"], safe_to_execute=False)

    pat = pattern.strip()
    os_key = _os_key()
    shell_cmd = f'grep -r "{pat}" .' if os_key != "windows" else f'findstr /s /r "{pat}" *'

    try:
        regex = re.compile(pat)
    except re.error:
        regex = re.compile(re.escape(pat))

    hits: List[str] = []
    try:
        for fpath in Path(".").rglob("*"):
            if not fpath.is_file():
                continue
            try:
                for i, line in enumerate(fpath.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{fpath}:{i}: {line.strip()}")
                        if len(hits) >= 200:  # cap results
                            break
            except Exception:
                continue
            if len(hits) >= 200:
                break

        output = "\n".join(hits) if hits else f"No matches found for: {pat}"
        return ToolResult(
            tool_name="search_text_in_files",
            arguments={"pattern": pat},
            rendered_commands=[shell_cmd],
            output=output,
            executed=True,
            safe_to_execute=True,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="search_text_in_files",
            arguments={"pattern": pat},
            rendered_commands=[shell_cmd],
            safe_to_execute=True,
            error=str(exc),
        )
