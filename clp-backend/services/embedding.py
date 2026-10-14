"""
Unified embedding module using a real sentence embedding model.

Provides a single ``get_embedding(text, task)`` entry point that:
  1. Tries the Gemini Embeddings API (if GEMINI_API_KEY is set).
  2. Falls back to a local ONNX execution of sentence-transformers/all-MiniLM-L6-v2 (384-dim).

Embeddings are used for:
  - **document**: building stored KB embeddings (via ``rebuild_embeddings``).
  - **query**: embedding user input at search time.
"""

from __future__ import annotations

import os
import logging
from typing import List, Literal, Optional

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer

logger = logging.getLogger(__name__)

TaskKind = Literal["document", "query"]

_ONNX_SESSION: Optional[ort.InferenceSession] = None
_TOKENIZER: Optional[AutoTokenizer] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_embedding(text: str, *, task: TaskKind = "document") -> List[float]:
    """
    Return an embedding vector for *text*.

    Parameters
    ----------
    text : str
        The text to embed.
    task : "document" | "query"
        ``document`` for KB entries (stored at index time),
        ``query`` for user input (used at search time).

    Returns
    -------
    List[float]
        Embedding vector.  Empty list if *text* is blank.
    """
    stripped = (text or "").strip()
    if not stripped:
        return []

    # Try Gemini first (if API key is available)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if api_key:
        try:
            return _get_gemini_embedding(stripped, task=task, api_key=api_key)
        except Exception as exc:
            logger.warning("Gemini embedding failed, falling back to local SBERT: %s", exc)

    # Fallback: local MiniLM-L6-v2 ONNX vector (works offline, no Net/API key required)
    try:
        return _get_local_embedding(stripped)
    except Exception as exc:
        logger.error("Local SBERT embedding failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Gemini embeddings
# ---------------------------------------------------------------------------

# "gemini-embedding-001" is the current production text embedding model.
# (The old "models/embedding-001" / "embedding-gecko-001" models are legacy
# PaLM-era models being phased out; "gemini-embedding-gecko-001" and
# "gemini-embedding-tiger-001" were never real model names, which is why
# calls were 404ing.)
_EMBED_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")

_GENAI_CLIENT = None


_GEMINI_TIMEOUT_MS = int(os.getenv("GEMINI_EMBEDDING_TIMEOUT_MS", "5000"))


def _get_genai_client():
    """Lazily construct and cache the google-genai client."""
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        from google import genai
        from google.genai.types import HttpOptions
        # http_options timeout ensures a slow/unresponsive API can't hang
        # the request indefinitely - without this, a network stall here
        # blocks the whole HTTP request with no fallback ever triggering.
        _GENAI_CLIENT = genai.Client(
            http_options=HttpOptions(timeout=_GEMINI_TIMEOUT_MS)
        )
    return _GENAI_CLIENT


def _get_gemini_embedding(
    text: str,
    *,
    task: TaskKind,
    api_key: str,
) -> List[float]:
    """Call the Gemini Embeddings API via the current google-genai SDK.

    Note: gemini-embedding-001 only accepts a single input string per
    request (unlike the older embedding-001 model, which allowed batches),
    so this function embeds one text at a time.
    """
    from google.genai.types import EmbedContentConfig

    client = _get_genai_client()

    task_type = "RETRIEVAL_DOCUMENT" if task == "document" else "RETRIEVAL_QUERY"

    result = client.models.embed_content(
        model=_EMBED_MODEL,
        contents=text,
        config=EmbedContentConfig(task_type=task_type),
    )

    embeddings = getattr(result, "embeddings", None)
    if embeddings:
        values = embeddings[0].values
        if values:
            logger.debug("Gemini embedding: %d dimensions", len(values))
            return [float(x) for x in values]

    raise ValueError("Gemini returned empty embedding")


# ---------------------------------------------------------------------------
# Local SBERT ONNX embeddings
# ---------------------------------------------------------------------------

def _get_local_embedding(text: str) -> List[float]:
    """
    Generate a 384-dimensional dense sentence embedding locally
    using Xenova/all-MiniLM-L6-v2 via ONNX runtime and CPU execution.
    """
    global _ONNX_SESSION, _TOKENIZER
    if _ONNX_SESSION is None or _TOKENIZER is None:
        # Suppress ONNX runtime logging warnings
        os.environ["ORT_LOGGING_LEVEL"] = "3"
        sess_options = ort.SessionOptions()
        sess_options.log_severity_level = 3

        # Download ONNX model file and tokenizer
        model_path = hf_hub_download(repo_id="Xenova/all-MiniLM-L6-v2", filename="onnx/model.onnx")

        _ONNX_SESSION = ort.InferenceSession(model_path, sess_options)
        _TOKENIZER = AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")

    # Tokenize input text
    encoded = _TOKENIZER(text, padding=True, truncation=True, return_tensors="np")
    inputs = {
        name: encoded[name].astype(np.int64)
        for name in ["input_ids", "attention_mask", "token_type_ids"]
        if name in encoded
    }

    # Run inference
    outputs = _ONNX_SESSION.run(None, inputs)
    last_hidden_state = outputs[0]
    attention_mask = encoded["attention_mask"]

    # Mean Pooling
    input_mask_expanded = np.expand_dims(attention_mask, axis=-1)
    input_mask_expanded = np.broadcast_to(input_mask_expanded, last_hidden_state.shape).astype(float)
    sum_embeddings = np.sum(last_hidden_state * input_mask_expanded, axis=1)
    sum_mask = np.clip(np.sum(input_mask_expanded, axis=1), a_min=1e-9, a_max=None)
    mean_embeddings = sum_embeddings / sum_mask

    # L2 Normalization
    norm = np.linalg.norm(mean_embeddings, ord=2, axis=1, keepdims=True)
    norm = np.clip(norm, a_min=1e-9, a_max=None)
    normalized_embeddings = mean_embeddings / norm

    return normalized_embeddings[0].tolist()


def clear_embedding_cache() -> None:
    """No-op for SBERT cache compatibility."""
    pass