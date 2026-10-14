"""
FastAPI server for CLP Terminal Agent.

Wraps the pipeline module so the React frontend (termix-cli) can call it over HTTP.

Run:
    cd clp-backend
    uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from dotenv import load_dotenv

# Ensure sibling packages (pipeline, knowledge/, tools/, etc.) are importable
_BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

# Load environment variables BEFORE any other imports that need them
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pipeline import resolve
from security.validator import validate_commands, contains_unresolved_placeholders
from execution.executor import execute_commands
from utils.history_logger import log_history
from knowledge.learner import save_command
from utils.project_detector import detect_project

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CLP Terminal Agent API",
    description="FastAPI server exposing the CLP terminal agent for the TermiX frontend.",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory session store
# ---------------------------------------------------------------------------

sessions: Dict[str, Dict[str, Any]] = {}

_DEFAULT_CWD = str(Path.home())  # Start every session in the user's home dir


def _session_cwd(session_id: str) -> str:
    """Return the current working directory for a session."""
    if session_id and session_id in sessions:
        return sessions[session_id].get("cwd") or _DEFAULT_CWD
    return _DEFAULT_CWD


# ---------------------------------------------------------------------------
# cd helper
# ---------------------------------------------------------------------------

_CD_PATTERN = re.compile(r"^\s*cd(?:\s+(.+))?\s*$", re.IGNORECASE)


def _handle_cd(command: str, current_cwd: str) -> Optional[dict]:
    """
    If `command` is a cd command, resolve the new directory and return
    a result dict.  Returns None if the command is not cd.
    """
    m = _CD_PATTERN.match(command)
    if m is None:
        return None

    raw_target = (m.group(1) or "").strip()

    # `cd` with no argument → go home
    if not raw_target:
        new_dir = str(Path.home())
    else:
        # Expand ~
        expanded = os.path.expanduser(raw_target)
        # Resolve relative paths against current session cwd
        if not os.path.isabs(expanded):
            expanded = os.path.join(current_cwd, expanded)
        new_dir = str(Path(expanded).resolve())

    if not os.path.isdir(new_dir):
        return {
            "success": False,
            "new_cwd": current_cwd,
            "error": f"cd: no such file or directory: {raw_target}",
        }

    return {
        "success": True,
        "new_cwd": new_dir,
        "error": "",
    }


# ---------------------------------------------------------------------------
# Source formatting
# ---------------------------------------------------------------------------

def _format_source(source: str) -> str:
    return {
        "intent": "Deterministic (Intent Handler)",
        "project": "Project Context (Auto-detected)",
        "kb_fuzzy": "Knowledge Base (Fuzzy Match)",
        "kb_semantic": "Knowledge Base (Semantic)",
        "llm": "LLM (Gemini)",
        "file_ops": "File Operations",
        "gemini": "LLM (Gemini)",
        "chat": "Chat (Out-of-Scope)",
    }.get(source, source or "Unknown")


def _chat_response() -> "ResolveResponse":
    return ResolveResponse(
        commands=[],
        source="chat",
        source_display="Chat (Out-of-Scope)",
        score=0.0,
        intent="unknown",
        requires_confirmation=False,
        error="I'm built for terminal and developer tasks. Try something like \"list files\", \"create folder demo\", or \"install pandas\".",
    )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ResolveRequest(BaseModel):
    query: str
    session_id: str = ""
    cwd: str = ""  # Frontend sends its current directory


class ResolveResponse(BaseModel):
    commands: List[str]
    source: str
    source_display: str
    score: float = 0.0
    intent: str
    requires_confirmation: bool = False
    tool_name: str = ""
    missing_placeholders: List[str] = []
    tool_output: str = ""
    error: str = ""
    cwd: str = ""  # Echo back so frontend can update its cwd state


class ExecuteRequest(BaseModel):
    session_id: str
    command: str
    source: str
    cwd: str = ""  # Overrides session cwd if provided


class ExecuteResponse(BaseModel):
    results: List[Dict[str, Any]]
    line: str = ""
    cwd: str = ""  # New cwd after execution (may change after cd)


class InputRequest(BaseModel):
    session_id: str
    input: str


class StatusResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "clp-terminal-agent"}


@app.get("/cwd")
async def get_default_cwd():
    """Return the default starting cwd (user home directory)."""
    return {"cwd": _DEFAULT_CWD}


@app.post("/resolve", response_model=ResolveResponse)
async def resolve_endpoint(req: ResolveRequest):
    """
    Phase 1: Resolve a natural-language query into commands.
    """
    instruction = (req.query or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Empty query")

    # Determine effective cwd for this session
    session_id = req.session_id
    effective_cwd = req.cwd.strip() or _session_cwd(session_id) or _DEFAULT_CWD
    effective_cwd = str(Path(os.path.expanduser(effective_cwd)).resolve())

    # Detect project context (non-fatal)
    try:
        project_ctx = detect_project(effective_cwd)
        project_hint = project_ctx.to_prompt_string()
    except Exception:
        project_hint = ""

    # Ensure embeddings are ready (non-fatal if it fails)
    try:
        from knowledge.semantic import maybe_auto_rebuild_embeddings
        maybe_auto_rebuild_embeddings()
    except Exception:
        pass

    try:
        resolved = resolve(
            instruction,
            session_id=session_id,
            cwd=effective_cwd,
            project_hint=project_hint,
        )
    except Exception as exc:
        logger.error("Pipeline resolve failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}")

    source = resolved.get("source") or ""

    if source == "chat":
        return _chat_response()

    commands = list(resolved.get("commands") or [])
    error = resolved.get("error") or ""
    score = float(resolved.get("score") or 0.0)
    intent = resolved.get("intent", "unknown") or "unknown"
    requires_confirmation = bool(resolved.get("requires_confirmation", False))
    tool_name = resolved.get("tool_name") or ""
    missing_placeholders = list(resolved.get("missing_placeholders") or [])
    tool_output = resolved.get("tool_output") or ""

    if error:
        return ResolveResponse(
            commands=[], source=source, source_display=_format_source(source),
            score=score, intent=intent, requires_confirmation=requires_confirmation,
            tool_name=tool_name, missing_placeholders=missing_placeholders,
            error=error, cwd=effective_cwd,
        )

    if missing_placeholders:
        return ResolveResponse(
            commands=[], source=source, source_display=_format_source(source),
            score=score, intent=intent, requires_confirmation=requires_confirmation,
            tool_name=tool_name, missing_placeholders=missing_placeholders,
            cwd=effective_cwd,
        )

    if not commands and not tool_output:
        return ResolveResponse(
            commands=[], source=source, source_display=_format_source(source),
            score=score, intent="unknown", requires_confirmation=False,
            error="No commands generated.", cwd=effective_cwd,
        )

    # Persist session context
    if session_id:
        session_data = sessions.get(session_id, {}).copy()
        session_data.update({
            "status": "resolved",
            "user_input": instruction,
            "commands": commands,
            "tool_output": tool_output,
            "tool_name": tool_name,
            "source": source,
            "requires_confirmation": requires_confirmation,
            "rule_id": resolved.get("rule_id"),
            "cwd": effective_cwd,
        })
        if resolved.get("pending_tool"):
            session_data["pending_tool"] = resolved.get("pending_tool")
        else:
            session_data.pop("pending_tool", None)
        sessions[session_id] = session_data

    return ResolveResponse(
        commands=commands, source=source, source_display=_format_source(source),
        score=score, intent=intent, requires_confirmation=requires_confirmation,
        tool_name=tool_name, missing_placeholders=missing_placeholders,
        tool_output=tool_output, cwd=effective_cwd,
    )


@app.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest):
    """
    Phase 2: Execute approved commands.

    Handles cd specially (updates session cwd without subprocess).
    Returns the new cwd in the response so the frontend can update its prompt.
    """
    session_id = req.session_id
    session = sessions.get(session_id) if session_id else None

    # Effective cwd: request override → session → default
    effective_cwd = (
        str(Path(os.path.expanduser(req.cwd)).resolve()) if req.cwd.strip()
        else (session.get("cwd") if session else None) or _DEFAULT_CWD
    )

    # ── Deferred Python-native tool ──────────────────────────────────────────
    if session and session.get("pending_tool"):
        from tools.tool_runner import execute_confirmed_tool

        pending_tool = session["pending_tool"]
        rule_id = pending_tool.get("rule_id", "")
        arguments = pending_tool.get("arguments", {})

        tool_result = execute_confirmed_tool(rule_id, arguments)
        rendered_cmd = tool_result.rendered_commands[0] if tool_result.rendered_commands else rule_id
        results = [{
            "command": rendered_cmd,
            "success": not bool(tool_result.error),
            "output": tool_result.output,
            "error": tool_result.error,
        }]

        try:
            log_history(session.get("user_input", ""), tool_result.rendered_commands or [rule_id], session.get("source", ""))
        except Exception:
            pass

        session["status"] = "completed"
        return ExecuteResponse(results=results, line=f"[executor] Deferred tool executed: {rule_id}", cwd=effective_cwd)

    # ── Standard execution path ──────────────────────────────────────────────
    command = (req.command or "").strip()
    if not command:
        raise HTTPException(status_code=400, detail="Empty command")

    commands_to_run = [c.strip() for c in command.split("|||") if c.strip()] if "|||" in command else [command]

    results: List[Dict[str, Any]] = []
    new_cwd = effective_cwd

    for cmd in commands_to_run:
        # ── Handle cd specially ──────────────────────────────────────────
        cd_result = _handle_cd(cmd, new_cwd)
        if cd_result is not None:
            new_cwd = cd_result["new_cwd"]
            results.append({
                "command": cmd,
                "success": cd_result["success"],
                "output": new_cwd if cd_result["success"] else "",
                "error": cd_result["error"],
            })
            if not cd_result["success"]:
                break
            continue

        # ── Block unsafe / unresolved placeholders ───────────────────────
        if contains_unresolved_placeholders(cmd):
            results.append({"command": cmd, "success": False, "output": "", "error": "Blocked unresolved placeholder(s)."})
            break

        if not validate_commands([cmd]):
            results.append({"command": cmd, "success": False, "output": "", "error": "Unsafe command blocked."})
            break

        # ── Execute ──────────────────────────────────────────────────────
        exec_results = execute_commands([cmd], cwd=new_cwd)
        results.extend(exec_results)
        if exec_results and not exec_results[-1].get("success"):
            break

    # Persist new cwd into session
    if session_id:
        existing = sessions.get(session_id, {})
        existing["cwd"] = new_cwd
        existing["status"] = "completed"
        sessions[session_id] = existing

    try:
        log_history(command, commands_to_run, req.source)
    except Exception:
        pass

    # Learn from successful LLM outputs
    if req.source in {"llm", "gemini"}:
        succeeded = [r["command"] for r in results if r.get("success")]
        learned_query = (session.get("user_input") or command) if session else command
        if succeeded and learned_query:
            try:
                save_command(learned_query, succeeded)
            except Exception:
                logger.exception("Failed to persist learned LLM command(s)")

    log_lines = []
    for r in results:
        log_lines.append(f"[executor] {'SUCCESS' if r.get('success') else 'FAILED'}: {r['command']}")

    return ExecuteResponse(results=results, line="\n".join(log_lines), cwd=new_cwd)


@app.post("/execute/stream")
async def execute_stream(req: ExecuteRequest):
    """
    Streaming execute via Server-Sent Events.

    The frontend connects with EventSource and receives output line-by-line
    as the command runs — essential for long-running commands like npm/pip.

    Events:
        data: <line>\\n\\n        — a line of stdout/stderr
        data: [CD] <new_path>\\n\\n — cd was handled, new cwd
        data: [DONE] <json>\\n\\n  — command finished: {"exit": int, "cwd": str}
        data: [ERROR] <msg>\\n\\n  — validation or other error
    """
    session_id = req.session_id
    session = sessions.get(session_id) if session_id else None

    effective_cwd = (
        str(Path(os.path.expanduser(req.cwd)).resolve()) if req.cwd.strip()
        else (session.get("cwd") if session else None) or _DEFAULT_CWD
    )

    command = (req.command or "").strip()

    async def event_stream() -> AsyncGenerator[str, None]:
        nonlocal effective_cwd
        commands_to_run = [c.strip() for c in command.split("|||") if c.strip()] if "|||" in command else [command]
        new_cwd = effective_cwd

        for cmd in commands_to_run:
            # cd handling
            cd_result = _handle_cd(cmd, new_cwd)
            if cd_result is not None:
                new_cwd = cd_result["new_cwd"]
                if cd_result["success"]:
                    yield f"data: [CD] {new_cwd}\n\n"
                else:
                    yield f"data: [ERROR] {cd_result['error']}\n\n"
                    break
                continue

            # Validation
            if contains_unresolved_placeholders(cmd):
                yield "data: [ERROR] Blocked: unresolved placeholder(s) in command.\n\n"
                break
            if not validate_commands([cmd]):
                yield "data: [ERROR] Blocked: unsafe command.\n\n"
                break

            # Stream subprocess output
            try:
                expanded_cwd = os.path.expanduser(new_cwd)
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=expanded_cwd,
                )

                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    yield f"data: {line}\n\n"

                exit_code = await proc.wait()

                if exit_code != 0:
                    done_payload = json.dumps({"exit": exit_code, "cwd": new_cwd, "success": False})
                    yield f"data: [DONE] {done_payload}\n\n"
                    break

            except Exception as exc:
                yield f"data: [ERROR] {exc}\n\n"
                break

        # Update session cwd
        if session_id:
            existing = sessions.get(session_id, {})
            existing["cwd"] = new_cwd
            existing["status"] = "completed"
            sessions[session_id] = existing

        done_payload = json.dumps({"exit": 0, "cwd": new_cwd, "success": True})
        yield f"data: [DONE] {done_payload}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class ExplainRequest(BaseModel):
    command: str
    output: str = ""
    error: str = ""
    cwd: str = ""


@app.post("/explain")
async def explain_error(req: ExplainRequest):
    """
    Given a failed command + its output, ask the LLM to explain what went
    wrong in plain English and suggest a concrete fix.
    Returns: { explanation: str, suggested_fix: str }
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return {"explanation": "GEMINI_API_KEY not set.", "suggested_fix": ""}

    prompt = (
        "You are a helpful terminal assistant. A developer ran a command and it failed.\n"
        f"Working directory: {req.cwd or 'unknown'}\n"
        f"Command: {req.command}\n"
        f"Output / Error:\n{(req.output or '') + (req.error or '')}\n\n"
        "In 2-3 sentences, explain what went wrong in plain English. "
        "Then on a new line starting with 'Fix:' give a specific, runnable command or action to resolve it."
    )

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        text = response.text.strip()

        # Split explanation from fix suggestion
        if "Fix:" in text:
            parts = text.split("Fix:", 1)
            explanation = parts[0].strip()
            suggested_fix = parts[1].strip()
        else:
            explanation = text
            suggested_fix = ""

        return {"explanation": explanation, "suggested_fix": suggested_fix}
    except Exception as exc:
        logger.warning("explain_error LLM call failed: %s", exc)
        return {"explanation": str(exc), "suggested_fix": ""}


@app.post("/input")
async def send_input(req: InputRequest):
    session = sessions.get(req.session_id)
    if not session:
        return {"line": "No active session.", "status": "completed"}
    return {"line": "", "status": session.get("status", "completed")}


@app.get("/status", response_model=StatusResponse)
async def status(session_id: str = ""):
    session = sessions.get(session_id)
    if not session:
        return StatusResponse(status="completed")
    return StatusResponse(status=session.get("status", "completed"))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    logger.info("CLP Terminal Agent API started on port 8000")
    logger.info("GEMINI_MODEL=%s", os.getenv("GEMINI_MODEL", "not set"))
    logger.info("Default session cwd: %s", _DEFAULT_CWD)
