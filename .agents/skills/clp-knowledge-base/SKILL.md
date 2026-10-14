---
name: clp-knowledge-base
description: Instructions for managing the CLP terminal assistant's knowledge base, adding/modifying rules, and rebuilding embeddings. Activate this skill when modifying knowledge_base.json or when running rebuild_embeddings.py.
---

# CLP Knowledge Base Management Skill

This skill guides the user and AI coding assistants on how to manage, edit, and keep the deterministic knowledge base up-to-date.

## 1. File Structure of `knowledge_base.json`

The knowledge base is located at `clp-backend/knowledge/knowledge_base.json`. It is a JSON file structured as follows:

```json
{
  "version": "1.1.0",
  "description": "...",
  "rules": [
    {
      "id": "unique_rule_identifier",
      "intent": "Short summary of the intent",
      "examples": [
        "example phrase 1",
        "example phrase with placeholder <parameter_name>",
        "shortcut_command"
      ],
      "description": "Description of what this command does.",
      "commands": {
        "linux": "command_for_linux <parameter_name>",
        "macos": "command_for_macos <parameter_name>",
        "windows": "command_for_windows <parameter_name>"
      },
      "risk_level": "low",
      "requires_confirmation": false,
      "tags": ["category1", "category2"]
    }
  ]
}
```

### Constraints & Guidelines
1. **Rule ID**: Must be unique and start with a prefix indicating its subsystem (e.g., `fs_` for filesystem, `git_` for git ops, `app_` for general applications).
2. **Commands**: Specify all target platforms. If a command is the same for macOS and Linux, duplicate it under both keys. For Windows, provide a CMD/PowerShell equivalent if possible (e.g. `type nul > <filename>` instead of `touch <filename>`).
3. **Risk Level**: Can be `"low"`, `"medium"`, or `"high"`.
4. **Requires Confirmation**: Must be `true` for destructive actions (like removing files, deleting folders, formatting disk, killing arbitrary processes).
5. **Placeholders**: Enclose variables in angle brackets, like `<filename>` or `<folder>`. Use the exact same placeholder name in both `examples` and `commands` strings so they can be parsed correctly by the placeholder extractor.

---

## 2. Rebuilding Embeddings

Whenever you edit `knowledge_base.json`, the semantic retriever must regenerate its sentence embeddings, or the new intents/examples will not be matchable.

### Instructions:
1. Navigate to the backend directory:
   ```bash
   cd clp-backend
   ```
2. Activate the python virtual environment:
   ```bash
   source .venv/bin/activate
   ```
3. Run the embeddings rebuilder script:
   ```bash
   python rebuild_embeddings.py
   ```
4. Verify output prints:
   ```text
   Rebuilding embeddings in .../knowledge_base.json...
   ✓ Embeddings rebuilt successfully!
   ```

---

## 3. Verification & Validation

After adding or modifying rules:
1. Validate JSON syntax using a tool or standard validation library:
   ```bash
   python -m json.tool clp-backend/knowledge/knowledge_base.json > /dev/null
   ```
2. Test resolution using the FastAPI API health/resolve endpoints or Python direct checks.
