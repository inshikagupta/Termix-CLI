"""
Semantic KB search (local cosine similarity over stored embeddings) and embedding rebuild.

This module is the **single** semantic search implementation.
It uses embeddings stored inline in each KB rule's ``embedding`` field
and compares them to a query embedding using cosine similarity.

No vector database is used — everything is JSON-based and local.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from services.embedding import get_embedding
from utils.similarity import cosine_similarity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Semantic threshold for search.
# Lowered to 0.45 for dense SBERT embeddings.
SEMANTIC_THRESHOLD = 0.45

_auto_rebuild_done = False


# ---------------------------------------------------------------------------
# Text construction for embeddings
# ---------------------------------------------------------------------------

def build_rule_text(rule: Dict[str, Any]) -> str:
    """
    Build natural-language text used for embedding a KB rule.

    Combines intent, description, and examples into a single string.
    If the rule already has a ``text`` field, prefer that.
    """
    # If rule already has a curated text field, use it directly
    text = rule.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    intent = (rule.get("intent") or "").strip()
    desc = (rule.get("description") or "").strip()
    parts: List[str] = []
    if intent:
        parts.append(intent)
    if desc:
        parts.append(desc)
    examples = rule.get("examples")
    if isinstance(examples, list) and examples:
        ex_strs = [str(e).strip() for e in examples if isinstance(e, str) and str(e).strip()]
        if ex_strs:
            parts.append("Examples: " + "; ".join(ex_strs))
    return ". ".join(parts) if parts else intent or desc or ""


# ---------------------------------------------------------------------------
# Embedding rebuild
# ---------------------------------------------------------------------------

def rebuild_embeddings(path: Optional[str] = None) -> None:
    """
    For each KB rule: ensure ``id``, ``text``, ``command`` (canonical), and ``embedding``.

    Writes the updated KB to disk and clears the in-memory KB cache.
    Embeddings are stored inline in the rule — no separate embeddings file.
    """
    from knowledge.retriever import canonical_kb_command, clear_knowledge_cache

    kb_path = path or _default_kb_path()
    with open(kb_path, "r", encoding="utf-8") as f:
        kb = json.load(f)

    rules = kb.get("rules") or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue

        # Ensure every rule has an ID
        rid = rule.get("id")
        if not isinstance(rid, str) or not rid.strip():
            rule["id"] = str(uuid.uuid4())

        # Build/refresh the text field
        text = build_rule_text(rule)
        rule["text"] = text

        # Store canonical command for quick lookup
        cmd = canonical_kb_command(rule)  # type: ignore[arg-type]
        if cmd:
            rule["command"] = cmd

        # Generate embedding
        if not text:
            rule["embedding"] = []
            continue

        try:
            rule["embedding"] = get_embedding(text, task="document")
        except Exception:
            logger.exception("Embedding failed for rule id=%s", rule.get("id"))
            rule["embedding"] = []

    kb["rules"] = rules
    with open(kb_path, "w", encoding="utf-8") as f:
        json.dump(kb, f, indent=2, ensure_ascii=False)
        f.write("\n")

    clear_knowledge_cache()
    logger.info("rebuild_embeddings: wrote %d rules to %s", len(rules), kb_path)


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SemanticHit:
    """Result of a successful semantic search."""
    command: str
    similarity: float
    rule: Dict[str, Any]


def semantic_search(
    user_input: str,
    *,
    kb: Optional[Dict[str, Any]] = None,
    threshold: float = SEMANTIC_THRESHOLD,
) -> Optional[SemanticHit]:
    """
    Embed ``user_input``, cosine-compare to each rule's inline ``embedding``,
    and return the best match above *threshold*.

    Returns None if no rule exceeds the threshold.
    """
    from knowledge.retriever import _pick_os_command, load_knowledge

    kb_obj = kb if kb is not None else load_knowledge()
    rules = kb_obj.get("rules") or []
    q = (user_input or "").strip()
    if not q:
        return None

    # Check that at least one rule has an embedding
    if not any(
        isinstance(r, dict)
        and isinstance(r.get("embedding"), list)
        and len(r["embedding"]) > 0
        for r in rules
    ):
        logger.debug("semantic_search: no rules have embeddings, skipping")
        return None

    # Embed the query
    try:
        query_vec = get_embedding(q, task="query")
    except Exception:
        logger.exception("semantic_search: query embedding failed")
        return None

    if not query_vec:
        return None

    # Find best match
    best_sim = -1.0
    best_rule: Optional[Dict[str, Any]] = None

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        emb = rule.get("embedding")
        if not isinstance(emb, list) or len(emb) == 0:
            continue
        sim = cosine_similarity(query_vec, emb)
        if sim > best_sim:
            best_sim = sim
            best_rule = rule

    if best_rule is None or best_sim < threshold:
        logger.debug(
            "semantic_search: best_sim=%.4f < threshold=%.2f, no match",
            best_sim, threshold,
        )
        return None

    cmd = _pick_os_command(best_rule)  # type: ignore[arg-type]
    if not cmd:
        return None

    return SemanticHit(command=cmd, similarity=best_sim, rule=best_rule)


# ---------------------------------------------------------------------------
# Auto-rebuild helper
# ---------------------------------------------------------------------------

def maybe_auto_rebuild_embeddings(path: Optional[str] = None) -> None:
    """
    Optionally rebuild embeddings once per process when ``CLP_AUTO_REBUILD_EMBEDDINGS=1``.
    """
    global _auto_rebuild_done
    if _auto_rebuild_done:
        return
    if os.getenv("CLP_AUTO_REBUILD_EMBEDDINGS", "").strip() != "1":
        return
    if not os.getenv("GEMINI_API_KEY", "").strip():
        logger.warning("CLP_AUTO_REBUILD_EMBEDDINGS set but GEMINI_API_KEY is missing; skip rebuild")
        _auto_rebuild_done = True
        return
    _auto_rebuild_done = True
    try:
        rebuild_embeddings(path=path)
    except Exception:
        logger.exception("auto rebuild_embeddings failed")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _default_kb_path() -> str:
    return os.path.join(os.path.dirname(__file__), "knowledge_base.json")
