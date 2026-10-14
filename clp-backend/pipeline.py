"""
CLP Resolution Pipeline.

Replaces the entire agent/ folder (graph.py, nodes.py, state.py).

A single ``resolve()`` function takes a raw user query and returns a
structured dict that server.py reads — the same shape that the old
graph.invoke() returned.

Flow:
    normalize → chat guard → retrieve → tool runner → validate → return

No external agent framework (LangGraph) is involved.
Everything is a plain sequential Python function.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from utils.normalizer import normalize_text
from utils.chat_classifier import is_chat_input
from security.validator import validate_commands, contains_unresolved_placeholders
from knowledge.retriever import retrieve, load_knowledge, _pick_os_command

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(
    user_input: str,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    project_hint: str = "",
) -> Dict[str, Any]:
    """
    Resolve a natural-language terminal query into executable commands.

    Parameters
    ----------
    user_input : str
        Raw user query.
    session_id : str, optional
        Session identifier (for logging).
    cwd : str, optional
        The session's current working directory.
    project_hint : str, optional
        Human-readable project context string injected into the LLM prompt,
        e.g. "Project type: node | Package manager: npm | Framework: react".
    """
    raw = (user_input or "").strip()
    state: Dict[str, Any] = _empty_state(raw, cwd)

    if not raw:
        state["error"] = "Empty query"
        return state

    # ── Step 1: Chat guard ──────────────────────────────────────────────────
    if is_chat_input(raw):
        state["source"] = "chat"
        logger.info("[pipeline] chat detected for: '%s'", raw)
        return state

    # ── Step 2: Normalize (for logging / KB match) ──────────────────────────
    normalized = normalize_text(raw)
    logger.debug("[pipeline] normalized: '%s'", normalized)

    # ── Step 3: Retrieve (4-tier pipeline: intent→fuzzy→semantic→LLM) ───────
    result = retrieve(raw, project_hint=project_hint, cwd=cwd)

    # Chat guard can also fire inside retriever — propagate it
    if result.source == "chat":
        state["source"] = "chat"
        return state

    state["source"] = result.source
    state["score"] = result.score
    state["intent"] = result.intent
    state["requires_confirmation"] = result.requires_confirmation
    state["rule_id"] = result.rule_id

    # ── Step 4: Tool runner (for KB-matched rules) ──────────────────────────
    if result.source in ("intent", "kb_intent", "kb_fuzzy", "kb_semantic") and result.rule_id:
        state = _run_tool_layer(state, result, raw, cwd=cwd)
        return state

    # ── Step 5: LLM / direct commands fallthrough ───────────────────────────
    if result.commands and validate_commands(result.commands):
        state["commands"] = result.commands
        state["validated"] = True
        logger.info("[pipeline] %s resolved %d command(s)", result.source, len(result.commands))
    elif result.commands:
        state["error"] = "Unsafe command detected"
        logger.warning("[pipeline] blocked commands from %s: %s", result.source, result.commands)
    else:
        if result.source == "llm":
            state["error"] = "LLM fallback failed or is unavailable. Check GEMINI_API_KEY / model response."
        else:
            state["error"] = "No commands generated"
        logger.warning("[pipeline] no commands from %s", result.source)

    return state


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _empty_state(raw: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    return {
        "user_input": raw,
        "commands": [],
        "source": "none",
        "score": 0.0,
        "intent": "unknown",
        "requires_confirmation": False,
        "tool_name": "",
        "tool_output": "",
        "missing_placeholders": [],
        "pending_tool": {},
        "rule_id": None,
        "execution_result": [],
        "validated": False,
        "error": "",
        "cwd": cwd,
    }


def _run_tool_layer(
    state: Dict[str, Any],
    result: Any,
    raw_query: str,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run the tool layer for a KB-matched rule.

    Mirrors the logic from the old knowledge_lookup_node() in agent/nodes.py.
    """
    from tools.tool_runner import run_tool

    # Resolve the OS-specific command template from the KB rule
    command_template = result.commands[0] if result.commands else ""
    kb_rule = None

    if result.rule_id:
        try:
            kb_data = load_knowledge()
            kb_rule = next(
                (r for r in (kb_data.get("rules") or []) if r.get("id") == result.rule_id),
                None,
            )
            if kb_rule:
                cmd = _pick_os_command(kb_rule)
                if cmd:
                    command_template = cmd
        except Exception as exc:
            logger.warning("[pipeline] could not load KB rule id=%s: %s", result.rule_id, exc)

    # Merge confirmation flag from KB rule if present
    requires_conf = result.requires_confirmation
    if kb_rule:
        requires_conf = requires_conf or kb_rule.get("requires_confirmation", False)
        state["requires_confirmation"] = requires_conf

    tool_result = run_tool(
        rule_id=result.rule_id,
        command_template=command_template,
        user_input=raw_query,
        kb_rule=kb_rule,
        requires_confirmation=requires_conf,
    )

    state["tool_name"] = tool_result.tool_name

    # Missing slots — stop here, let the frontend prompt the user
    if tool_result.missing_placeholders:
        state["missing_placeholders"] = tool_result.missing_placeholders
        logger.info(
            "[pipeline] missing slots %s for rule=%s",
            tool_result.missing_placeholders, result.rule_id,
        )
        return state

    # Unsafe tool result
    if not tool_result.safe_to_execute:
        state["error"] = tool_result.error or "Tool execution blocked as unsafe"
        logger.warning("[pipeline] blocked tool=%s: %s", tool_result.tool_name, tool_result.error)
        return state

    # Python-native op already executed
    if tool_result.executed:
        state["tool_output"] = tool_result.output
        state["execution_result"] = [{
            "command": command_template,
            "success": not bool(tool_result.error),
            "output": tool_result.output,
            "error": tool_result.error,
        }]
        state["validated"] = True
        state["commands"] = []
        state["missing_placeholders"] = []
        state["pending_tool"] = {}
        logger.info("[pipeline] Python-native tool executed: %s", tool_result.tool_name)
        return state

    # Shell command rendered — validate it
    cmds = tool_result.rendered_commands or []
    if validate_commands(cmds):
        state["commands"] = cmds
        state["validated"] = True
        state["missing_placeholders"] = []
        state["requires_confirmation"] = tool_result.requires_confirmation

        # Deferred destructive ops need a pending_tool payload
        if tool_result.requires_confirmation and result.rule_id in {"fs_remove_file", "fs_remove_folder"}:
            state["pending_tool"] = {
                "rule_id": result.rule_id,
                "arguments": tool_result.arguments,
            }

        logger.info(
            "[pipeline] tool=%s rendered=%s confirmation=%s",
            tool_result.tool_name, cmds, tool_result.requires_confirmation,
        )
    else:
        state["error"] = tool_result.error or "Unsafe command detected"
        logger.warning("[pipeline] tool blocked: %s", tool_result.error)

    return state
