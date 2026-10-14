"""
Unified hybrid retrieval pipeline for NL-to-command resolution.

This is the **single canonical retrieval module**.  Every query flows through
the same 4-tier pipeline in strict order:

    1. Deterministic intent handlers  (file-create, etc.)   → source="intent"
    2. Fuzzy KB match                 (RapidFuzz ≥ 0.85)    → source="kb_fuzzy"
    3. Semantic similarity match      (cosine sim ≥ 0.60)   → source="kb_semantic"
    4. LLM fallback                   (Gemini generation)   → source="llm"

Each tier short-circuits: the first tier to produce a result wins.

Public API:
    retrieve(user_input)          → RetrievalResult
    load_knowledge(path)          → KnowledgeBase (cached)
    clear_knowledge_cache()       → None
    canonical_kb_command(rule)    → Optional[str]
    search_command(query)         → List[str]    (backward-compat convenience)

No vector database is used.  Embeddings are stored inline in the KB JSON.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, TypedDict

from utils.fuzzy_match import match_query
from utils.normalizer import normalize_text
from utils.os_detector import detect_os
from utils.chat_classifier import is_chat_input

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Thresholds — edit these to tune routing behaviour
# ═══════════════════════════════════════════════════════════════════════════

# Fuzzy: RapidFuzz score in 0-100 range.  90 means near-exact match
# on example strings.  Lower → more false positives.
FUZZY_THRESHOLD = 90.0

# Semantic: cosine similarity in 0-1 range.  0.45 is a good middle ground
# for dense embeddings from all-MiniLM-L6-v2.
SEMANTIC_THRESHOLD = 0.45

# Commands matching these patterns require user confirmation before execution.
_DANGEROUS_PATTERNS = re.compile(
    r"\b(rm\s+-rf|rmdir|del\s|format|mkfs|dd\s|shutdown|reboot)\b",
    re.IGNORECASE,
)

_FILENAME_LIKE_RE = re.compile(r"\b[\w.-]+\.[A-Za-z0-9]{1,8}\b")
_QUOTED_TEXT_RE = re.compile(r"""["']([^"']+)["']""")
_INSTALL_TARGET_RE = re.compile(r"\b(?:pip|npm)\s+install\s+([a-z0-9_.@/-]+)\b", re.IGNORECASE)
_SIMPLE_INSTALL_RE = re.compile(r"\binstall\s+([a-z0-9_.@/-]+)\b", re.IGNORECASE)

_PYTHON_PACKAGES = {"pandas", "numpy", "fastapi", "requests", "flask", "django"}
_NODE_PACKAGES = {"react", "express", "axios", "vite", "next", "typescript"}
_INSTALL_STOPWORDS = {"it", "this", "that", "them", "package", "library", "module"}


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

class KnowledgeRule(TypedDict, total=False):
    id: str
    text: str
    embedding: List[float]
    intent: str
    description: str
    examples: List[str]
    commands: Dict[str, str]
    command: str  # backward compat with older KB entries


class KnowledgeBase(TypedDict, total=False):
    version: str
    description: str
    rules: List[KnowledgeRule]


@dataclass
class RetrievalResult:
    """Structured output from the retrieval pipeline."""

    commands: List[str]                 # Shell commands to execute
    source: str                          # "intent" | "kb_fuzzy" | "kb_semantic" | "llm" | "none"
    score: float                         # 0.0–1.0 normalised confidence
    intent: str = "general"              # "file_op" | "general" | "unknown"
    requires_confirmation: bool = False  # True for destructive / LLM-sourced commands
    rule_id: Optional[str] = None        # KB rule ID for traceability


# ═══════════════════════════════════════════════════════════════════════════
# KB loading
# ═══════════════════════════════════════════════════════════════════════════

def _default_kb_path() -> str:
    return os.path.join(os.path.dirname(__file__), "knowledge_base.json")


def clear_knowledge_cache() -> None:
    """Invalidate cached KB after on-disk updates (e.g. embedding rebuild)."""
    load_knowledge.cache_clear()


@lru_cache(maxsize=1)
def load_knowledge(path: Optional[str] = None) -> KnowledgeBase:
    """
    Load the JSON knowledge base from disk.

    Cached in-memory to avoid per-call I/O.
    """
    kb_path = path or _default_kb_path()
    with open(kb_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("knowledge_base.json must contain a JSON object at the root.")
    if "rules" not in data or not isinstance(data["rules"], list):
        raise ValueError('knowledge_base.json must contain a "rules": [] list.')
    return data  # type: ignore[return-value]


# ═══════════════════════════════════════════════════════════════════════════
# OS-aware command helpers
# ═══════════════════════════════════════════════════════════════════════════

def _map_os_for_kb() -> str:
    """Map ``detect_os()`` output to KB command keys (linux / macos / windows)."""
    current = detect_os()
    if current == "mac":
        return "macos"
    if current == "windows":
        return "windows"
    return "linux"


def canonical_kb_command(rule: KnowledgeRule) -> Optional[str]:
    """
    Stable command string for KB storage.

    Prefers ``linux`` from the ``commands`` map, then any available key,
    then the legacy ``command`` field.
    """
    commands_map = rule.get("commands")
    if isinstance(commands_map, dict):
        chosen = commands_map.get("linux")
        if isinstance(chosen, str) and chosen.strip():
            return chosen.strip()
        for _k, v in commands_map.items():
            if isinstance(v, str) and v.strip():
                return v.strip()

    legacy = rule.get("command")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    return None


def _pick_os_command(rule: KnowledgeRule) -> Optional[str]:
    """
    Resolve command from OS-specific map with fallback to linux.

    Also supports legacy single ``command`` field.
    """
    commands_map = rule.get("commands")
    if isinstance(commands_map, dict):
        os_key = _map_os_for_kb()
        chosen = commands_map.get(os_key) or commands_map.get("linux")
        if isinstance(chosen, str) and chosen.strip():
            return chosen.strip()

    # Backward compatibility with older KB schema entries.
    legacy = rule.get("command")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Confirmation logic
# ═══════════════════════════════════════════════════════════════════════════

def _tier_chat_guard(user_input: str) -> Optional[RetrievalResult]:
    """
    Tier -1: Detect and block greetings / small talk before processing.
    
    Returns a RetrievalResult with source="chat" to signal the frontend
    that this is not a terminal command, just a friendly response.
    """
    if not is_chat_input(user_input):
        return None
    
    logger.info("[retrieve] ✓ Chat detected: '%s' → blocking from pipeline", user_input)
    return RetrievalResult(
        commands=[],
        source="chat",
        score=1.0,
        intent="unknown",
        requires_confirmation=False,
        rule_id=None,
    )


def _needs_confirmation(commands: List[str], source: str) -> bool:
    """
    Decide whether the user should confirm before execution.

    Rules:
    - LLM-sourced commands always require confirmation (not from KB).
    - Any command matching a destructive pattern requires confirmation.
    """
    if source == "llm":
        return True
    return any(_DANGEROUS_PATTERNS.search(cmd) for cmd in commands)


def _rule_by_id(kb: KnowledgeBase, rule_id: str) -> Optional[KnowledgeRule]:
    rules = kb.get("rules") or []
    for rule in rules:
        if isinstance(rule, dict) and rule.get("id") == rule_id:
            return rule  # type: ignore[return-value]
    return None


def _result_from_rule(rule: KnowledgeRule, *, source: str, score: float) -> Optional[RetrievalResult]:
    cmd = _pick_os_command(rule)
    if not cmd:
        return None
    commands = [cmd]
    return RetrievalResult(
        commands=commands,
        source=source,
        score=score,
        intent="file_op" if str(rule.get("id", "")).startswith("fs_") else "general",
        requires_confirmation=_needs_confirmation(commands, source),
        rule_id=rule.get("id"),
    )


def _tier_deterministic_git(text: str) -> Optional[str]:
    """Route git CLI commands deterministically."""
    tokens = text.split()
    if not tokens or tokens[0] != "git":
        return None

    if len(tokens) == 1:
        return None

    cmd = tokens[1]
    if cmd == "status":
        return "git_status"
    elif cmd == "diff":
        return "git_diff"
    elif cmd == "log":
        return "git_log"
    elif cmd == "init":
        return "git_init"
    elif cmd == "clone":
        return "git_clone"
    elif cmd == "pull":
        return "git_pull"
    elif cmd == "push":
        return "git_push"
    elif cmd == "remote":
        return "git_remote"
    elif cmd == "add":
        return "git_add_all"
    elif cmd == "commit":
        if "add" in tokens:
            return "git_add_commit"
        return "git_commit"
    elif cmd == "checkout":
        if "b" in tokens or "new" in tokens or "create" in tokens:
            return "git_branch_create"
        return "git_checkout_branch"
    elif cmd == "branch":
        if len(tokens) == 2:
            return "git_branch_list"
        if "d" in tokens or "delete" in tokens:
            return None
        return "git_branch_create"
    elif cmd == "stash":
        if "pop" in tokens or "apply" in tokens:
            return "git_stash_pop"
        return "git_stash"
    return None


def _tier_deterministic_cli_commands(text: str, raw_input: str) -> Optional[RetrievalResult]:
    """Route obvious CLI commands directly to KB rules without fuzzy/semantic matching."""
    tokens = text.split()
    if not tokens:
        return None

    cmd = tokens[0]
    target_rule_id: Optional[str] = None

    # Git
    if cmd == "git":
        target_rule_id = _tier_deterministic_git(text)

    # Python
    elif cmd in ("python", "python3"):
        target_rule_id = "python_run_file"

    # Pip
    elif cmd in ("pip", "pip3"):
        if len(tokens) >= 2:
            sub = tokens[1]
            if sub == "uninstall":
                target_rule_id = "pip_uninstall"
            elif sub == "list":
                target_rule_id = "pip_list"
            elif sub == "freeze":
                target_rule_id = "pip_freeze"
            elif sub == "install":
                if "requirements" in tokens or "requirementstxt" in tokens or "-r" in raw_input:
                    target_rule_id = "pip_install_requirements"
                else:
                    target_rule_id = "python_pip_install"

    # npm
    elif cmd == "run" and len(tokens) >= 2:
        if "dev" in tokens:
            target_rule_id = "npm_run_dev"
        elif "build" in tokens:
            target_rule_id = "npm_run_build"
        elif any(t.endswith(".py") for t in tokens) or "python" in tokens or "py" in tokens:
            target_rule_id = "python_run_file"
        else:
            known_scripts = {"start", "test", "lint", "preview", "serve", "watch", "format", "eject"}
            if set(tokens) & known_scripts:
                target_rule_id = "npm_run_script"
            else:
                target_rule_id = None

    # kill pid / kill port
    elif cmd == "kill":
        if "port" in tokens:
            target_rule_id = "sys_kill_port"
        elif any(w in tokens for w in ("process", "pid", "processid")):
            target_rule_id = "sys_kill_process"
        elif any(t.isdigit() for t in tokens if not t.startswith("-")):
            target_rule_id = "sys_kill_process"
        elif not any(w in tokens for w in ("node", "python", "nginx", "service", "processes")):
            target_rule_id = "sys_kill_process"

    # ping
    elif cmd == "ping":
        target_rule_id = "sys_ping_host"

    # uninstall / remove / delete (e.g. "remove lodash" or "uninstall requests")
    elif cmd in ("uninstall", "remove", "delete") and len(tokens) >= 2:
        pkg = tokens[1]
        # Ignore if it is a directory/file remove (e.g. "delete folder <folder>" or "remove file.txt")
        if not any(w in tokens for w in ("file", "folder", "dir", "directory")) and not bool(_FILENAME_LIKE_RE.search(raw_input)):
            if pkg in _PYTHON_PACKAGES:
                target_rule_id = "pip_uninstall"
            else:
                target_rule_id = "npm_uninstall"

    # run dev / run build / run lint / run script
    elif cmd == "run" and len(tokens) >= 2:
        if "dev" in tokens:
            target_rule_id = "npm_run_dev"
        elif "build" in tokens:
            target_rule_id = "npm_run_build"
        elif any(t.endswith(".py") for t in tokens) or "python" in tokens or "py" in tokens:
            target_rule_id = "python_run_file"
        else:
            target_rule_id = "npm_run_script"

    if target_rule_id:
        kb = load_knowledge()
        rule = _rule_by_id(kb, target_rule_id)
        if rule:
            return _result_from_rule(rule, source="intent", score=1.0)

    return None


def _tier_deterministic_intent(normalized_input: str, raw_input: str) -> Optional[RetrievalResult]:
    """
    Route obvious structured filesystem intents directly to KB rule IDs.
    This prevents fuzzy false positives like mapping "create folder" to "pwd".
    """
    text = normalized_input.strip().lower()
    if not text:
        return None

    has_filename_like = bool(_FILENAME_LIKE_RE.search(raw_input))

    create_words = {"create", "make", "new", "touch"}
    folder_create_words = {"create", "make", "new", "mkdir"}
    remove_words = {"delete", "remove", "rm"}
    remove_folder_words = {"delete", "remove", "rmdir"}
    copy_words = {"copy", "cp"}
    move_words = {"move", "mv", "rename"}
    read_words = {"read", "show", "cat", "view"}
    find_words = {"find", "locate"}
    search_words = {"grep", "search"}

    tokens = set(text.split())
    has_file_word = "file" in tokens
    has_folder_word = any(w in tokens for w in {"folder", "directory", "dir"})

    target_rule_id: Optional[str] = None

    # Order matters: folder/file remove/create are close semantically.
    if ("rm -rf" in raw_input) or ("rm -r" in raw_input) or ("rmdir" in tokens):
        target_rule_id = "fs_remove_folder"
    elif (tokens & remove_folder_words) and has_folder_word:
        target_rule_id = "fs_remove_folder"
    elif (tokens & remove_words) and (has_file_word or has_filename_like):
        target_rule_id = "fs_remove_file"
    elif (tokens & folder_create_words) and has_folder_word:
        target_rule_id = "fs_create_folder"
    elif (tokens & create_words) and has_file_word:
        target_rule_id = "fs_create_file"
    elif text.startswith("cd ") or text == "cd" or ("change" in tokens and "directory" in tokens):
        target_rule_id = "fs_change_directory"
    elif (tokens & {"go", "navigate", "switch"}) and (
        has_folder_word or ("to" in tokens) or
        ({"back", "up", "previous", "parent"} & tokens)
    ):
        target_rule_id = "fs_change_directory"
    elif tokens & copy_words:
        target_rule_id = "fs_copy_file"
    elif tokens & move_words:
        target_rule_id = "fs_move_file"
    elif (
        (tokens & search_words)
        or ("find text" in text)
        or ("look for text" in text)
        or ("pattern" in tokens)
    ):
        target_rule_id = "fs_search_text_in_files"
    elif (
        (tokens & find_words and has_filename_like)
        or ("where is" in text and has_filename_like)
    ):
        target_rule_id = "fs_find_file_by_name"
    elif (tokens & read_words) and (has_file_word or has_filename_like):
        target_rule_id = "fs_show_file_contents"

    if not target_rule_id:
        return None

    kb = load_knowledge()
    rule = _rule_by_id(kb, target_rule_id)
    if not rule:
        return None
    return _result_from_rule(rule, source="intent", score=1.0)


def _extract_install_target(normalized_input: str) -> str:
    m = _INSTALL_TARGET_RE.search(normalized_input)
    if m:
        return m.group(1).strip()
    m = _SIMPLE_INSTALL_RE.search(normalized_input)
    if m:
        candidate = m.group(1).strip()
        if candidate not in _INSTALL_STOPWORDS:
            return candidate

    # If the sentence mentions a known package elsewhere, prefer that over
    # pronouns like "install it".
    tokens = normalized_input.split()
    known_packages = _PYTHON_PACKAGES | _NODE_PACKAGES
    for token in tokens:
        if token in known_packages:
            return token
    return ""


def _tier_deterministic_install_intent(normalized_input: str) -> Optional[RetrievalResult]:
    """
    Route developer package-install queries before fuzzy matching.
    Routes to the right specific KB rule (npm_install_package, npm_install_dev,
    pip_install_requirements, python_pip_install).
    """
    text = normalized_input.strip().lower()
    if "install" not in text or "uninstall" in text:
        return None

    # "install requirements" or "install from requirements.txt" → pip_install_requirements
    if re.search(r"\brequirements?(\.txt)?\b", text):
        kb = load_knowledge()
        rule = _rule_by_id(kb, "pip_install_requirements")
        if rule:
            return _result_from_rule(rule, source="intent", score=1.0)
        return RetrievalResult(
            commands=["pip install -r requirements.txt"],
            source="intent", score=1.0, intent="general",
            requires_confirmation=False, rule_id="pip_install_requirements",
        )

    target = _extract_install_target(text)
    if not target:
        return None

    tokens = set(text.split())
    is_dev = bool(re.search(
        r"(?:--save-dev|as\s+(?:a\s+)?dev|dev\s+dep(?:endenc)?|devdep|for\s+dev(?:elopment)?)",
        text, re.I
    ))
    ecosystem: Optional[str] = None
    if "pip" in tokens or "python" in tokens:
        ecosystem = "python"
    elif "npm" in tokens or "node" in tokens:
        ecosystem = "node"
    elif target in _PYTHON_PACKAGES:
        ecosystem = "python"
    elif target in _NODE_PACKAGES:
        ecosystem = "node"
    elif is_dev:
        ecosystem = "node"  # dev dependencies are virtually always npm

    if ecosystem is None:
        return None

    kb = load_knowledge()
    if ecosystem == "python":
        rule = _rule_by_id(kb, "python_pip_install")
        if rule:
            result = _result_from_rule(rule, source="intent", score=1.0)
            if result:
                return result
        return RetrievalResult(
            commands=[f"pip install {target}"],
            source="intent", score=1.0, intent="general",
            requires_confirmation=False, rule_id=None,
        )

    # Node: dev vs regular
    if is_dev:
        rule = _rule_by_id(kb, "npm_install_dev")
        if rule:
            cmd = f"npm install --save-dev {target}"
            return RetrievalResult(
                commands=[cmd], source="intent", score=1.0, intent="general",
                requires_confirmation=False, rule_id="npm_install_dev",
            )

    rule = _rule_by_id(kb, "npm_install_package")
    if rule:
        cmd = f"npm install {target}"
        return RetrievalResult(
            commands=[cmd], source="intent", score=1.0, intent="general",
            requires_confirmation=False, rule_id="npm_install_package",
        )

    return RetrievalResult(
        commands=[f"npm install {target}"],
        source="intent", score=1.0, intent="general",
        requires_confirmation=False, rule_id=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════

def retrieve(user_input: str, project_hint: str = "", cwd: Optional[str] = None) -> RetrievalResult:
    """
    Resolve a natural-language query into shell commands.

    Pipeline (short-circuits on first match):
        0. Chat guard           (greetings/small-talk)   → source="chat"
        0.5 Project-aware       (dev/test/lint/build)    → source="project"
        1. Fuzzy KB match       → source="kb_fuzzy"
        2. Semantic similarity  → source="kb_semantic"
        3. LLM fallback         → source="llm"

    project_hint is injected into the LLM prompt to give context about
    the current project (e.g. "Project type: node | Framework: react").
    cwd enables project-aware routing from package.json / Makefile / stack.
    """
    raw = (user_input or "").strip()
    if not raw:
        return RetrievalResult(commands=[], source="none", score=0.0, intent="unknown")

    # ── Pre-Tier: Chat Guard (Greetings/Small-talk) ────────────────────
    logger.debug("[retrieve] Chat guard check for '%s'", raw)
    chat_result = _tier_chat_guard(raw)
    if chat_result:
        return chat_result

    normalized = normalize_text(raw)

    # ── Tier 0: Deterministic routing for obvious filesystem intents ──────
    logger.debug("[retrieve] Tier 0 (deterministic install/cli/intent) for '%s'", normalized)
    install_result = _tier_deterministic_install_intent(normalized)
    if install_result:
        logger.info("[retrieve] ✓ Tier 0 (deterministic install) command=%s", install_result.commands)
        return install_result

    cli_result = _tier_deterministic_cli_commands(normalized, raw)
    if cli_result:
        logger.info("[retrieve] ✓ Tier 0 (deterministic cli) rule=%s command=%s", cli_result.rule_id, cli_result.commands)
        return cli_result

    # ── Tier 0.5: Project-aware routing (tests, lint, dev, build) ─────────
    if cwd:
        from utils.project_router import route_project_query

        logger.debug("[retrieve] Tier 0.5 (project-aware) for '%s' cwd=%s", normalized, cwd)
        project_result = route_project_query(normalized, cwd=cwd)
        if project_result:
            logger.info(
                "[retrieve] ✓ Tier 0.5 (project) rule=%s command=%s",
                project_result.rule_id,
                project_result.commands,
            )
            return project_result

    intent_result = _tier_deterministic_intent(normalized, raw)
    if intent_result:
        logger.info("[retrieve] ✓ Tier 0 (deterministic intent) rule=%s", intent_result.rule_id)
        return intent_result

    # Install queries without ecosystem hints are too broad for fuzzy matching.
    # Route them straight to LLM so we do not incorrectly map "install docker"
    # to package-manager-specific KB rules like npm/pip install.
    if "install" in normalized:
        target = _extract_install_target(normalized)
        if target:
            logger.info("[retrieve] install intent ambiguous for '%s' → forcing LLM fallback", raw)
            return _tier_llm(raw, project_hint=project_hint)

    # Vague git commit requests are workflow-like and fuzzy matching tends to
    # misroute them to unrelated filesystem rules. Let the LLM handle them.
    if _should_force_llm(normalized, raw):
        logger.info("[retrieve] forcing LLM fallback for workflow-like query: %s", raw)
        return _tier_llm(raw, project_hint=project_hint)

    # ── Tier 1: Fuzzy KB match ───────────────────────────────────────
    logger.debug("[retrieve] Tier 1 (fuzzy) for '%s'", normalized)
    fuzzy_result = _tier_fuzzy(normalized)
    if fuzzy_result:
        logger.info("[retrieve] ✓ Tier 1 (fuzzy) score=%.2f", fuzzy_result.score)
        return fuzzy_result

    # ── Tier 2: Semantic similarity ────────────────────────────────────
    logger.debug("[retrieve] Tier 2 (semantic) for '%s'", normalized)
    semantic_result = _tier_semantic(raw)
    if semantic_result:
        logger.info("[retrieve] ✓ Tier 2 (semantic) score=%.2f", semantic_result.score)
        return semantic_result

    # ── Tier 3: LLM fallback ────────────────────────────────────────
    logger.debug("[retrieve] Tier 3 (LLM) for '%s'", normalized)
    llm_result = _tier_llm(raw, project_hint=project_hint)
    logger.info("[retrieve] ✓ Tier 3 (LLM) generated %d command(s)", len(llm_result.commands))
    return llm_result


def _should_force_llm(normalized_input: str, raw_input: str) -> bool:
    """
    Detect high-level workflow queries that should skip KB fuzzy matching.

    Minimal targeted guard:
    - "commit the changes" and similar queries lack the commit message needed
      by the KB rule, and fuzzy matching often misroutes them to fs_* rules.
    """
    tokens = set(normalized_input.split())
    if "commit" not in tokens:
        return False

    has_commit_message = bool(_QUOTED_TEXT_RE.search(raw_input)) or "message" in tokens
    if has_commit_message:
        return False

    return "change" in tokens or "changes" in tokens



# ═══════════════════════════════════════════════════════════════════════════
# Tier implementations
# ═══════════════════════════════════════════════════════════════════════════


def _tier_fuzzy(normalized_input: str) -> Optional[RetrievalResult]:
    """
    Tier 2: Fuzzy matching against KB examples and intent strings.

    Uses RapidFuzz (WRatio scorer).  Threshold: FUZZY_THRESHOLD (85).
    Score is normalised to 0–1 for the output.
    """
    try:
        kb = load_knowledge()
        rules = kb.get("rules") or []

        best_rule: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for rule in rules:
            if not isinstance(rule, dict):
                continue

            # Prefer high-signal fields (examples + intent). Description/text
            # are intentionally excluded to reduce broad fuzzy false positives.
            candidates: List[str] = []
            for ex in rule.get("examples") or []:
                if isinstance(ex, str) and ex.strip():
                    norm_ex=normalize_text(ex)
                    if len(norm_ex) < len(normalized_input) * 0.4 and norm_ex != normalized_input:
                        continue
                    candidates.append(normalize_text(ex))
            for field_val in (rule.get("intent"),):
                if isinstance(field_val, str) and field_val.strip():
                    candidates.append(normalize_text(field_val))

            if not candidates:
                continue

            _match, score = match_query(normalized_input, candidates)
            if float(score) > best_score:
                best_score = float(score)
                best_rule = rule

        if best_rule is None or best_score < FUZZY_THRESHOLD:
            return None

        cmd = _pick_os_command(best_rule)  # type: ignore[arg-type]
        if not cmd:
            return None

        commands = [cmd]
        return RetrievalResult(
            commands=commands,
            source="kb_fuzzy",
            score=best_score / 100.0,  # Normalise RapidFuzz 0-100 → 0-1
            intent="general",
            requires_confirmation=_needs_confirmation(commands, "kb_fuzzy"),
            rule_id=best_rule.get("id"),
        )

    except Exception as exc:
        logger.error("[retrieve] Fuzzy matching failed: %s", exc)
        return None


def _tier_semantic(raw_input: str) -> Optional[RetrievalResult]:
    """
    Tier 3: Semantic search using embeddings + cosine similarity.

    Uses embeddings stored inline in KB rules.  Query is embedded
    at search time using the same model (Gemini or TF fallback).
    Threshold: SEMANTIC_THRESHOLD (0.60).
    """
    try:
        from knowledge.semantic import semantic_search

        kb = load_knowledge()
        hit = semantic_search(raw_input, kb=kb, threshold=SEMANTIC_THRESHOLD)

        if hit is None:
            return None

        commands = [hit.command]
        return RetrievalResult(
            commands=commands,
            source="kb_semantic",
            score=hit.similarity,
            intent="general",
            requires_confirmation=_needs_confirmation(commands, "kb_semantic"),
            rule_id=hit.rule.get("id"),
        )

    except Exception as exc:
        logger.error("[retrieve] Semantic search failed: %s", exc)
        return None


def _tier_llm(raw_input: str, project_hint: str = "") -> RetrievalResult:
    """
    Tier 4: LLM fallback for novel / unknown requests.
    project_hint is appended to the prompt so the LLM knows the project type.
    """
    import json
    import os

    try:
        from services.llm import call_llm, safe_parse

        extra_context = f"\nProject context: {project_hint}" if project_hint else ""
        enriched_input = f"{raw_input}{extra_context}"
        raw_response = call_llm(enriched_input)
        obj = safe_parse(raw_response, retries=2)

        commands: list = []
        if isinstance(obj, dict) and isinstance(obj.get("commands"), list):
            commands = [c.strip() for c in obj["commands"] if isinstance(c, str) and c.strip()]

        return RetrievalResult(
            commands=commands,
            source="llm",
            score=0.5,
            intent="general",
            requires_confirmation=True,  # Always confirm LLM output
        )
    except Exception as exc:
        logger.error("[retrieve] LLM generation failed: %s", exc)
        return RetrievalResult(
            commands=[],
            source="llm",
            score=0.0,
            intent="unknown",
            requires_confirmation=False,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Backward-compatible convenience wrappers
# ═══════════════════════════════════════════════════════════════════════════

def search_command(
    query: str,
    *,
    kb: Optional[KnowledgeBase] = None,
) -> List[str]:
    """
    Find commands for a user query.

    Convenience wrapper that returns just the command list.
    """
    return list(retrieve(query).commands)
