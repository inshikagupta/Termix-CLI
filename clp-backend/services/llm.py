from google import genai
import os
import json

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3-flash-preview")


def call_llm(user_input: str) -> str:
    system_prompt = f"""
You are an AI terminal command generator.

STRICT RULES:
- Output ONLY valid JSON
- No markdown, no explanations outside JSON
- Commands must be safe and minimal

Format:
{{
  "commands": ["command1", "command2"],
  "explanation": "short explanation"
}}

User Input:
{user_input}
"""

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=system_prompt
        )
        return response.text.strip()
    except Exception as e:
        raise RuntimeError(f"Gemini Error: {str(e)}") from e


def safe_parse(output: str, retries: int = 2) -> dict:
    """
    Parse model output as JSON with lightweight repair retries.
    """
    current = output
    for _ in range(retries + 1):
        try:
            return json.loads(current)
        except Exception:
            current = call_llm(
                "Fix this JSON. Return ONLY valid JSON with no markdown:\n" + current
            )
    raise ValueError("LLM returned invalid JSON")

