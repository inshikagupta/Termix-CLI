"""
Lightweight greeting detector.

Only blocks very basic greeting-like inputs so normal task requests
continue through the command pipeline.
"""

from __future__ import annotations

from typing import Set

# ═══════════════════════════════════════════════════════════════════════════
# Exact greeting strings only
# ═══════════════════════════════════════════════════════════════════════════

_EXACT_CHAT_PHRASES: Set[str] = {
    "hello",
    "hi",
    "hii",
    "hey",
    "welcome",
    "good morning",
    "good afternoon",
    "good evening",
    "good night",
    "how are you",
    "how's it going",
    "what's up",
    "sup",
    "yo",
    "howdy",
    "greetings",
    "hey there",
    "tell me about yourself",
    "who are you",
    "what are you",
    "what's your name",
    "what do you do",
    "help me",
    "can you help"
}


def is_chat_input(text: str) -> bool:
    """
    Detect if input is a very basic greeting.

    Args:
        text: User input string.

    Returns:
        True if the input is detected as a basic greeting, False otherwise.

    Examples:
        >>> is_chat_input("hello")
        True
        >>> is_chat_input("create folder demo")
        False
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return False

    normalized = cleaned.lower().rstrip("!?.,")
    return normalized in _EXACT_CHAT_PHRASES
