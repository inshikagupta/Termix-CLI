"""
Project-aware command routing.

Maps high-level developer intents ("run tests", "start dev server", "lint")
to concrete shell commands using detected project metadata from
``project_detector.detect_project()``.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from knowledge.retriever import RetrievalResult
from utils.project_detector import ProjectContext, detect_project

# ── Intent patterns (order matters within each group) ───────────────────────

_DEV_PATTERNS = (
    re.compile(r"\b(?:run|start)\s+(?:the\s+)?(?:dev(?:elopment)?(?:\s+server)?|dev\s+server)\b", re.I),
    # Was: only matched "start ... app/application/project" literally.
    # Now: matches "run"/"start" ... "app"/"application"/"project", allowing
    # any words in between (e.g. "run this project", "run this node project").
    re.compile(r"\b(?:run|start)\b.*\b(?:app|application|project)\b", re.I),
    re.compile(r"^\s*dev\s*$", re.I),
)

_TEST_PATTERNS = (
    re.compile(r"\b(?:run|execute|start)\s+(?:the\s+)?(?:unit\s+)?tests?\b", re.I),
    re.compile(r"\b(?:run|execute)\s+(?:the\s+)?test\s+suite\b", re.I),
    re.compile(r"^\s*tests?\s*$", re.I),
)

_LINT_PATTERNS = (
    re.compile(r"\b(?:run|check|fix)\s+(?:the\s+)?lint(?:er|ing)?\b", re.I),
    re.compile(r"\blint\s+(?:the\s+)?(?:project|code|files)\b", re.I),
    re.compile(r"^\s*lint\s*$", re.I),
)

_BUILD_PATTERNS = (
    re.compile(r"\b(?:run|do|execute)\s+(?:a\s+)?build\b", re.I),
    re.compile(r"\bbuild\s+(?:the\s+)?(?:project|app|application)\b", re.I),
    re.compile(r"^\s*build\s*$", re.I),
)

# Explicit "run <script>" when script exists in package.json
_RUN_SCRIPT_RE = re.compile(
    r"^\s*(?:run|start)\s+(?P<script>[\w:-]+)\s*$",
    re.I,
)


def route_project_query(
    normalized_input: str,
    cwd: Optional[str] = None,
    project_ctx: Optional[ProjectContext] = None,
) -> Optional[RetrievalResult]:
    """
    Resolve developer workflow queries using project context.

    Returns a RetrievalResult with source="project" when a match is found.
    """
    text = (normalized_input or "").strip().lower()
    if not text:
        return None

    ctx = project_ctx or (detect_project(cwd) if cwd else ProjectContext(project_type="unknown", package_manager=""))
    if ctx.project_type == "unknown" and not ctx.scripts:
        return None

    # Explicit script name: "run electron:dev" when script exists
    m = _RUN_SCRIPT_RE.match(text)
    if m:
        script = m.group("script")
        if script in ctx.scripts:
            cmd = ctx.script_command(script)
            if cmd:
                return _project_result(cmd, intent=f"run script {script}", rule_id="project_run_script")

    resolvers: list[tuple[tuple[re.Pattern[str], ...], Callable[[ProjectContext], Optional[str]], str, str]] = [
        (_TEST_PATTERNS, ProjectContext.resolve_test_command, "run tests", "project_run_test"),
        (_LINT_PATTERNS, ProjectContext.resolve_lint_command, "lint project", "project_run_lint"),
        (_DEV_PATTERNS, ProjectContext.resolve_dev_command, "start dev server", "project_run_dev"),
        (_BUILD_PATTERNS, ProjectContext.resolve_build_command, "build project", "project_run_build"),
    ]

    for patterns, resolver, intent_label, rule_id in resolvers:
        if not any(p.search(text) for p in patterns):
            continue
        cmd = resolver(ctx)
        if cmd:
            return _project_result(cmd, intent=intent_label, rule_id=rule_id)

    return None


def _project_result(command: str, *, intent: str, rule_id: str) -> RetrievalResult:
    return RetrievalResult(
        commands=[command],
        source="project",
        score=1.0,
        intent=intent,
        requires_confirmation=False,
        rule_id=rule_id,
    )
