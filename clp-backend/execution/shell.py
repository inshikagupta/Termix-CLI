"""
Shell abstraction.

Wraps subprocess execution with OS-specific behaviour, safety validation,
and an optional working-directory override so session-based `cd` works.
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
from typing import List, Optional

from security.validator import validate_commands


class Shell:
    def __init__(self, *, timeout: int = 30, live_logging: bool = False) -> None:
        self.timeout = timeout
        self.live_logging = live_logging

    def _is_windows(self) -> bool:
        return platform.system().lower().startswith("windows")

    def _tokenize_command(self, command: str) -> List[str]:
        return shlex.split(command, posix=not self._is_windows())

    def _validate_command(self, command: str) -> bool:
        if not command or not command.strip():
            return False
        return validate_commands([command])

    def run(self, command: str, cwd: Optional[str] = None) -> dict:
        """
        Execute a command and return structured output.

        Parameters
        ----------
        command : str
            The shell command to run.
        cwd : str, optional
            Working directory for the subprocess. If None, inherits the
            server process's cwd. Pass session["cwd"] for user sessions.
        """
        if self.live_logging:
            print(f"[shell] Running: {command}  (cwd={cwd or os.getcwd()})")

        try:
            if not command or not command.strip():
                return {
                    "success": False,
                    "output": "",
                    "error": "Empty command.",
                    "return_code": -1,
                }

            # Expand ~ in the working directory
            effective_cwd = os.path.expanduser(cwd) if cwd else None

            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=True,
                cwd=effective_cwd,
            )
            return {
                "success": completed.returncode == 0,
                "output": completed.stdout or "",
                "error": completed.stderr or "",
                "return_code": int(completed.returncode),
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return {
                "success": False,
                "output": stdout,
                "error": f"Command timed out after {self.timeout}s. {stderr}".strip(),
                "return_code": -1,
            }
        except Exception as exc:
            return {
                "success": False,
                "output": "",
                "error": str(exc),
                "return_code": -1,
            }

    def run_safe(self, command: str, cwd: Optional[str] = None) -> dict:
        """Validate command before execution."""
        if not self._validate_command(command):
            return {
                "success": False,
                "output": "",
                "error": "Unsafe or invalid command blocked by validator.",
                "return_code": -1,
            }
        return self.run(command, cwd=cwd)
