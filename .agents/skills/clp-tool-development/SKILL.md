---
name: clp-tool-development
description: Instructions for creating and registering custom deterministic Python-native tool handlers in the CLP backend. Activate this skill when writing new tools or mapping rules to tool handlers.
---

# CLP Tool Development Skill

This skill guides you on how to extend the local terminal chatbot with custom Python-native tool handlers.

## 1. Tool Layer Architecture

When a query is resolved to a `rule_id` from the knowledge base, the execution flows as follows:
1. **Deferred confirmation check**: If the rule has `requires_confirmation = true`, execution is deferred until approved by the frontend.
2. **Registry lookup**: The `/execute` endpoint checks if the `rule_id` exists in `clp-backend/tools/tool_registry.py`.
   - **If found in registry**: It runs the registered Python callable directly, passing in extracted arguments. This is a **Python-native tool** (e.g. creating files, listing directory).
   - **If NOT found in registry**: It falls back to the **shell renderer** (defined in `clp-backend/tools/tool_runner.py`). The placeholder values are injected into the OS-specific shell command templates from the rule definition, and executed as raw shell commands.

---

## 2. Step-by-Step Tool Implementation

To implement a new Python-native tool:

### Step A: Write the Python handler
Place your logic in a file under `clp-backend/tools/` or a related submodule (like `clp-backend/filesystem/`).
The handler should return a `ToolResult` from `tools.types`.

```python
from tools.types import ToolResult

def tool_custom_action(arg1: str, arg2: str = "") -> ToolResult:
    """
    Perform a custom action.
    """
    try:
        # 1. Execute logic
        # 2. Return result
        return ToolResult(
            output="Successful outcome message",
            error="",
            rendered_commands=[f"custom_cmd --arg1 {arg1}"] # Command preview to show in logs
        )
    except Exception as e:
        return ToolResult(
            output="",
            error=str(e),
            rendered_commands=[]
        )
```

### Step B: Register the handler
Open `clp-backend/tools/tool_registry.py`:
1. Import your handler function:
   ```python
   from tools.my_module import tool_custom_action
   ```
2. Bind the rule ID to your handler in `TOOL_REGISTRY`:
   ```python
   TOOL_REGISTRY: Dict[str, HandlerFn] = {
       ...
       "my_custom_rule_id": lambda args: tool_custom_action(
           args.get("arg1", ""),
           args.get("arg2", "")
       ),
   }
   ```

### Step C: Add/Update the rule in the Knowledge Base
Ensure `clp-backend/knowledge/knowledge_base.json` has a rule with `id = "my_custom_rule_id"`.
Run the embedding rebuilder:
```bash
cd clp-backend
source .venv/bin/activate
python rebuild_embeddings.py
```

---

## 3. Testing and Verification

Ensure the custom tool behaves correctly through the endpoints:
1. **Resolve Test**: `/resolve` should correctly detect the intent, extract placeholders, and return `"tool_name": "my_custom_rule_id"`.
2. **Execute Test**: `/execute` should capture the returned `tool_output` or `execution_result` and log it.
