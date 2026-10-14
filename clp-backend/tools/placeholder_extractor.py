"""
Deterministic placeholder extractor.

Extracts <slot> values from natural-language user queries using
ordered regex strategies — no LLM involved.

Supported placeholders (matching the KB schema):
    <filename>      - a file name (with or without extension)
    <folder>        - a directory name
    <source>        - source path for copy/move operations
    <destination>   - destination path for copy/move operations
    <app_name>      - application name (validated downstream by allowlist)
    <pattern>       - text pattern for grep/search operations
    <branch>        - git branch name
    <message>       - git commit message
    <pid>           - process ID (integer)
    <host>          - hostname or IP address
    <package>       - pip/npm package name
    <script>        - Python script filename
    <port>          - TCP port number
    <url>           - URL (e.g. git clone URL)
    <script_name>   - npm run script name (dev, build, test, start ...)
    <process>       - process name for pkill/killall

Resolution contract:
    - If a value is confidently extracted  → it appears in ExtractedArgs.values
    - If a value cannot be confidently extracted → it appears in ExtractedArgs.missing
    - Callers must check `missing` before rendering commands;
      do NOT substitute empty strings or invented values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set


# ── Compiled regex helpers ──────────────────────────────────────────────────

# Quoted string: "value" or 'value'
_QUOTED_RE = re.compile(r"""["']([^"']+)["']""")

# A path-like token: alphanumeric + dots, slashes, underscores, hyphens, tilde
_PATH_TOKEN_RE = re.compile(r"""[~]?[\w./\\-]+\.\w+|[~][/\\][\w./\\-]*|[~]?[\w./\\-]{2,}""")

# Filename: has an extension  (e.g. notes.txt, archive.tar.gz)
_FILENAME_EXT_RE = re.compile(r"""\b([\w.-]+\.[\w]+)\b""")

# Bare name after cue words
_NAMED_RE = re.compile(r"""\b(?:named?|called)\s+([~\w./\\-]+)""", re.I)
_AFTER_FILE_RE = re.compile(r"""\b(?:create|make|touch|new)\b.*?\bfile\b\s+([~\w./\\-]+)""", re.I)
_AFTER_FOLDER_RE = re.compile(
    r"""\b(?:create|make|mkdir|new|remove|delete|rmdir)\b.*?\b(?:folder|directory|dir)\b\s+([~\w./\\-]+)""",
    re.I,
)

# Direct mkdir <name> (no 'folder' keyword): "mkdir myproject on Desktop"
_MKDIR_DIRECT_RE = re.compile(
    r"""\b(?:mkdir|rmdir)\s+([~\w./\\-]+)""",
    re.I,
)

# cd <path>: extract the path argument directly from cd commands
# Handles: cd ~/Desktop  cd ..  cd ~  cd /tmp  cd some/relative/path
_CD_PATH_RE = re.compile(
    r"""^\s*cd\s+([~./]?[~\w./\\-]*)\s*$""",
    re.I,
)

# Navigation cues: "go to Desktop", "navigate to ~/Documents", "move to /tmp"
_NAV_CUE_RE = re.compile(
    r"""\b(?:go\s+to|navigate\s+to|switch\s+to|move\s+to|open\s+folder|change\s+(?:to|directory\s+to))\s+(?:folder\s+)?([~\w./\\-]+)""",
    re.I,
)

# "go back" / "go up" / "previous directory" → ..
_GO_BACK_RE = re.compile(
    r"""\b(?:go\s+back|go\s+up|go\s+to\s+(?:the\s+)?(?:previous|parent|last|upper)\s+(?:folder|directory|dir|level)|"""
    r"""navigate\s+(?:back|up)|"""
    r"""(?:previous|parent)\s+(?:folder|directory|dir)|"""
    r"""back\s+(?:to\s+(?:the\s+)?)?(?:previous|parent)\s+(?:folder|directory)?|"""
    r"""one\s+level\s+up|"""
    r"""up\s+one\s+(?:level|folder|directory)|"""
    r"""cd\s+\.\.)""",
    re.I,
)


# Location cue: "on Desktop", "in ~/Documents", "at /some/path", "on the Desktop"
# Captures well-known shorthand names (Desktop, Downloads, Documents, etc.) and full paths
_LOCATION_CUE_RE = re.compile(
    r"""\b(?:on|in|at|inside|to|into)\s+(?:the\s+)?([~\w./\\-]+)""",
    re.I,
)

# Friendly location name → expanded path fragment
_LOCATION_MAP = {
    "desktop": "~/Desktop",
    "downloads": "~/Downloads",
    "documents": "~/Documents",
    "home": "~",
    "tmp": "/tmp",
    "temp": "/tmp",
}

# Source / destination: "X to Y"
_SRC_DEST_RE = re.compile(r"""([\w./\\-]+)\s+to\s+([\w./\\-]+)""", re.I)

# App name: after open/launch/start
_APP_NAME_RE = re.compile(
    r"""\b(?:open|launch|start)\s+(?:the\s+app\s+)?([\w\s]+?)
    (?:\s+(?:here|in\s+this(?:\s+\w+)?|in\s+the\s+current\s+(?:directory|dir|folder)|in\s+the))?$""",
    re.I | re.X,
)

# Pattern after "for" / "text" / "string" (for grep)
_PATTERN_AFTER_FOR_RE = re.compile(r"""\bfor\s+["']?(\S+)["']?""", re.I)
_PATTERN_AFTER_TEXT_RE = re.compile(r"""\b(?:text|string|pattern|grep)\s+["']?(\S+)["']?""", re.I)

# Branch name: after "branch" / "checkout"
_BRANCH_RE = re.compile(r"""\b(?:branch|checkout)\s+([\w./-]+)""", re.I)

# Commit message: quoted or after "saying"/"message"
_MSG_SAYING_RE = re.compile(r"""\b(?:saying|with\s+message?)\s+["']?(.+?)["']?$""", re.I)

# PID: plain integer, possibly after "pid" / "process" (negative lookbehind for flags like -9)
_PID_RE = re.compile(r"""\b(?:pid\s+|process\s+)?(?<!-)(\d+)\b""")

# Host: domain or IP-like token
_HOST_RE = re.compile(r"""\b((?:[\w-]+\.)+[\w-]{2,}|(?:\d{1,3}\.){3}\d{1,3})\b""")

# Package: after "install" / "add" / "uninstall" / "remove" etc. (supports scoped packages)
_PACKAGE_RE = re.compile(r"""\b(?:install|add|uninstall|remove|delete|rm)\s+(?:python\s+package\s+|package\s+)?["']?([\w@./-]+)["']?""", re.I)

# Script: *.py file
_SCRIPT_RE = re.compile(r"""\b([\w.-]+\.py)\b""")


# ── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class ExtractedArgs:
    """Result of placeholder extraction."""
    values: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)


# ── Individual extractors ───────────────────────────────────────────────────

def _extract_quoted(text: str) -> List[str]:
    return _QUOTED_RE.findall(text)


def _expand_location(location_name: str) -> str:
    """
    Expand shorthand location names to full paths.
    E.g. "Desktop" → "~/Desktop", "~/Documents" stays as-is.
    """
    stripped = location_name.strip()
    lower = stripped.lower()
    # If it's already a path (starts with ~ or /), return as-is
    if stripped.startswith("~") or stripped.startswith("/"):
        return stripped
    return _LOCATION_MAP.get(lower, stripped)


def _extract_filename(text: str) -> str:
    """Extract a filename from user text. Returns '' if not found."""
    # 1. Quoted string
    for q in _extract_quoted(text):
        q = q.strip()
        if q and not q.isspace():
            return q

    # 2. Filename with extension (may include a path prefix like ~/Desktop/)
    m = _FILENAME_EXT_RE.search(text)
    if m:
        # Check if there's a location cue that should prefix this filename
        loc_m = _LOCATION_CUE_RE.search(text)
        if loc_m:
            location = _expand_location(loc_m.group(1))
            bare_name = m.group(1)
            # If the location itself already contains a file extension, it IS the full path
            # e.g. "at ~/Desktop/notes.txt" → location="~/Desktop/notes.txt", bare_name="notes.txt"
            if "." in location.split("/")[-1]:
                return location
            # Avoid double-path if the filename already has a dir component
            if "/" not in bare_name and "\\" not in bare_name:
                return f"{location}/{bare_name}"
        return m.group(1)

    # 3. "named X" / "called X"
    m = _NAMED_RE.search(text)
    if m:
        name = m.group(1)
        loc_m = _LOCATION_CUE_RE.search(text)
        if loc_m:
            location = _expand_location(loc_m.group(1))
            if "/" not in name and "\\" not in name:
                return f"{location}/{name}"
        return name

    # 4. "create file X", "touch X"
    m = _AFTER_FILE_RE.search(text)
    if m:
        return m.group(1)

    return ""


def _extract_folder(text: str) -> str:
    """Extract a folder/directory name from user text."""
    # Words that are location prepositions, not actual folder names
    _LOCATION_PREPOSITIONS = {"on", "in", "at", "inside", "to", "into", "the"}

    # Quoted first
    for q in _extract_quoted(text):
        q = q.strip()
        if q:
            return q

    # 1. Direct cd path: "cd ~/Desktop", "cd ..", "cd ~", "cd /tmp"
    m = _CD_PATH_RE.match(text)
    if m:
        return _expand_location(m.group(1))

    # 2. "go back" / "go up" → parent directory
    if _GO_BACK_RE.search(text):
        return ".."

    # 3. Navigation cue: "go to Desktop", "navigate to ~/Documents"
    m = _NAV_CUE_RE.search(text)
    if m:
        return _expand_location(m.group(1))

    # 4. "named X" / "called X"
    m = _NAMED_RE.search(text)
    if m:
        name = m.group(1)
        loc_m = _LOCATION_CUE_RE.search(text)
        if loc_m:
            location = _expand_location(loc_m.group(1))
            # Don't double up if name IS the location already
            if name.lower() not in _LOCATION_MAP:
                return f"{location}/{name}"
        return name

    # 5. "create folder X on Desktop" — try _AFTER_FOLDER_RE and _MKDIR_DIRECT_RE, then location
    m2 = _AFTER_FOLDER_RE.search(text)
    folder_name_from_re = m2.group(1).strip() if m2 else ""

    if not folder_name_from_re:
        m3 = _MKDIR_DIRECT_RE.search(text)
        folder_name_from_re = m3.group(1).strip() if m3 else ""

    # If the captured folder name is just a location preposition, ignore it
    if folder_name_from_re.lower() in _LOCATION_PREPOSITIONS:
        folder_name_from_re = ""

    loc_m = _LOCATION_CUE_RE.search(text)
    if loc_m:
        location = _expand_location(loc_m.group(1))
        if folder_name_from_re and "/" not in folder_name_from_re and "\\" not in folder_name_from_re:
            return f"{location}/{folder_name_from_re}"
        return location

    # No location cue — use bare folder name from regex
    if folder_name_from_re:
        return folder_name_from_re

    # Raw CLI fallback for rm -rf / rmdir commands (e.g. "rm -rf my_folder")
    tokens = text.split()
    if tokens:
        cmd = tokens[0].lower()
        if cmd == "rmdir":
            return tokens[-1]
        elif cmd == "rm" and any(arg in tokens for arg in ("-rf", "-r", "-f", "-R")):
            args = [t for t in tokens[1:] if not t.startswith("-")]
            if args:
                return args[-1]

    return ""


def _extract_source_destination(text: str) -> tuple[str, str]:
    """
    Extract source and destination from phrases like:
      "copy report.pdf to backups/"
      "move old.py to archive/new.py"
    Returns (source, destination), either may be empty.
    """
    m = _SRC_DEST_RE.search(text)
    if m:
        return m.group(1), m.group(2)

    # Fallback: two quoted strings
    quoted = _extract_quoted(text)
    if len(quoted) >= 2:
        return quoted[0].strip(), quoted[1].strip()

    # Raw CLI fallback for cp/mv commands (e.g. "cp file.txt dest.txt")
    tokens = text.split()
    if tokens:
        cmd = tokens[0].lower()
        if cmd in ("cp", "mv"):
            args = [t for t in tokens[1:] if not t.startswith("-")]
            if len(args) >= 2:
                return args[0], args[1]

    return "", ""


def _extract_app_name(text: str) -> str:
    """Extract app name after open/launch/start."""
    # Quoted
    for q in _extract_quoted(text):
        return q.strip()

    m = _APP_NAME_RE.search(text)
    if m:
        return m.group(1).strip().lower()

    return ""


def _extract_pattern(text: str) -> str:
    """Extract search pattern for grep-style queries."""
    # Quoted string preferred
    for q in _extract_quoted(text):
        return q.strip()

    # Special handling for raw grep commands to skip flags
    tokens = text.split()
    if tokens:
        tokens_lower = [t.lower() for t in tokens]
        if "grep" in tokens_lower:
            try:
                idx = tokens_lower.index("grep")
                # Look for the first token after grep that does not start with -
                # and is not a common search boundary/directory target like '.', '*', './'
                for t in tokens[idx + 1:]:
                    if not t.startswith("-") and t not in (".", "*", "./", "../"):
                        return t.strip("'\"")
            except ValueError:
                pass

    m = _PATTERN_AFTER_FOR_RE.search(text)
    if m:
        return m.group(1)

    m = _PATTERN_AFTER_TEXT_RE.search(text)
    if m:
        return m.group(1)

    return ""


def _extract_branch(text: str) -> str:
    m = _BRANCH_RE.search(text)
    return m.group(1) if m else ""


def _extract_message(text: str) -> str:
    # Quoted string is the clearest signal
    for q in _extract_quoted(text):
        return q.strip()

    m = _MSG_SAYING_RE.search(text)
    return m.group(1).strip() if m else ""


def _extract_pid(text: str) -> str:
    m = _PID_RE.search(text)
    return m.group(1) if m else ""


def _extract_host(text: str) -> str:
    m = _HOST_RE.search(text)
    return m.group(1) if m else ""


def _extract_package(text: str) -> str:
    m = _PACKAGE_RE.search(text)
    return m.group(1) if m else ""


def _extract_script(text: str) -> str:
    m = _SCRIPT_RE.search(text)
    if m:
        return m.group(1)
    # Also try generic filename extractor
    return _extract_filename(text)

def _extract_port(text: str) -> str:
    """Extract a port number: 'kill port 3000', 'what's on port 8080', 'port 443'."""
    m = re.search(r"\bport\s+(\d{2,5})\b", text, re.I)
    if m:
        return m.group(1)
    # bare number after kill/check
    m = re.search(r"\b(?:kill|check|free|stop|use|on)\s+(\d{2,5})\b", text, re.I)
    if m:
        return m.group(1)
    # last standalone number if it looks like a port
    m = re.search(r"\b(\d{2,5})\b", text)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 65535:
            return m.group(1)
    return ""


def _extract_url(text: str) -> str:
    """Extract a URL (git clone, curl, wget, etc.)."""
    m = re.search(r"(https?://[\w./_~:?#@!$&'()*+,;=%-]+|git@[\w.-]+:[\w./-]+\.git)", text, re.I)
    return m.group(1) if m else ""


def _extract_script_name(text: str) -> str:
    """Extract npm run script name: 'npm run dev', 'run the build script', 'start script'."""
    # Direct: npm run <name>
    m = re.search(r"\bnpm\s+run\s+([\w:-]+)", text, re.I)
    if m:
        return m.group(1)
    # 'run <name> script' / 'run the <name> script'
    m = re.search(r"\brun\s+(?:the\s+)?([\w:-]+)\s+script", text, re.I)
    if m:
        return m.group(1)
    # 'run script <name>'
    m = re.search(r"\bscript\s+([\w:-]+)", text, re.I)
    if m:
        return m.group(1)
    # well-known script names as bare words
    m = re.search(r"\b(dev|build|start|test|lint|preview|serve|watch|format|eject)\b", text, re.I)
    if m:
        return m.group(1).lower()
    return ""


def _extract_process(text: str) -> str:
    """Extract process/service name for pkill: 'kill node processes', 'stop nginx'."""
    for q in _extract_quoted(text):
        return q.strip()
    # 'kill/stop/terminate <name> process(es)'
    m = re.search(r"\b(?:kill|stop|terminate|end)\s+([\w.-]+)\s+(?:process|processes|service)", text, re.I)
    if m:
        return m.group(1)
    # 'kill all <name>'
    m = re.search(r"\b(?:kill|stop)\s+all\s+([\w.-]+)", text, re.I)
    if m:
        return m.group(1)
    # 'pkill <name>'
    m = re.search(r"\bpkill\s+(?:-f\s+)?([\w.-]+)", text, re.I)
    if m:
        return m.group(1)
    return ""



_EXTRACTORS = {
    "filename":    _extract_filename,
    "folder":      _extract_folder,
    "app_name":    _extract_app_name,
    "pattern":     _extract_pattern,
    "branch":      _extract_branch,
    "message":     _extract_message,
    "pid":         _extract_pid,
    "host":        _extract_host,
    "package":     _extract_package,
    "script":      _extract_script,
    "port":        _extract_port,
    "url":         _extract_url,
    "script_name": _extract_script_name,
    "process":     _extract_process,
}


# ── Public API ──────────────────────────────────────────────────────────────

def detect_placeholders(command_template: str) -> List[str]:
    """
    Return a list of placeholder names found in a command template string.
    E.g. "touch <filename>" → ["filename"]
    """
    return re.findall(r"<(\w+)>", command_template)


def extract_placeholders(user_input: str, required: List[str]) -> ExtractedArgs:
    """
    Extract values for the given placeholder names from *user_input*.

    Parameters
    ----------
    user_input : str
        The raw user query.
    required : List[str]
        List of placeholder names to extract (e.g. ["filename"], ["source", "destination"]).

    Returns
    -------
    ExtractedArgs
        .values  — successfully extracted {name: value} pairs
        .missing — names for which no value could be confidently extracted
    """
    text = (user_input or "").strip()
    values: Dict[str, str] = {}
    missing: List[str] = []

    # Special case: source + destination need joint extraction
    if "source" in required or "destination" in required:
        src, dst = _extract_source_destination(text)
        if "source" in required:
            if src:
                values["source"] = src
            else:
                missing.append("source")
        if "destination" in required:
            if dst:
                values["destination"] = dst
            else:
                missing.append("destination")

    for name in required:
        if name in ("source", "destination"):
            continue  # already handled above
        extractor = _EXTRACTORS.get(name)
        if extractor is None:
            # Unknown placeholder — can't extract, mark missing
            missing.append(name)
            continue
        value = extractor(text)
        if value:
            values[name] = value
        else:
            missing.append(name)

    return ExtractedArgs(values=values, missing=missing)
