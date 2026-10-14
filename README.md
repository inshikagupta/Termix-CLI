# TermiX — AI Terminal Assistant

TermiX turns plain English into safe, runnable terminal commands. Type `create a folder hello`, `install pandas` or `kill port 3000`, review the command it resolves, and run it in a real working directory with live streamed output.

The project has two parts:

| Folder | What it is | Stack |
|---|---|---|
| [`clp-backend/`](clp-backend) | CLP Terminal Agent: resolves natural language to commands, validates them, and executes them | Python, FastAPI, RapidFuzz, ONNX embeddings, Google Gemini |
| [`termix-cli/`](termix-cli) | Terminal-style desktop/web UI | React 18, Vite 5, Electron 28 |

---

## Features

- **Natural language to shell commands**, with direct shell commands also supported.
- **4-tier resolution pipeline** that tries cheap, deterministic methods first and only calls an LLM as a last resort.
- **Safety gate**: deny-list for destructive patterns (`rm -rf /`, `mkfs`, `dd`, fork bombs, `chmod 777 /`, `kill -9 -1`, disk overwrites), blocking of unresolved `<placeholders>`, and an explicit confirmation step for risky operations.
- **Project awareness**: detects Node, Python, Rust and Go projects (package manager, framework, scripts, test runner, Git and Docker) so "run tests", "start dev server", "lint" and "build" map to the right command.
- **Real working directory**: `cd` is handled per session and the prompt updates accordingly.
- **Live output streaming** over Server-Sent Events, useful for `npm install` or `pip install`.
- **Error explanation**: ask Gemini to explain a failed command and suggest a fix.
- **Self-learning knowledge base**: successful LLM-generated commands are saved for future lookups.
- **Cross-platform command templates** for macOS, Linux and Windows.
- **Themeable UI** (green, amber, blue, red, cyan), command history, and keyboard shortcuts.

---

## How It Works

```
User query
   │
   ▼
Chat guard ── greeting / small talk? ──► friendly "I'm built for terminal tasks" reply
   │
   ▼
Normalize
   │
   ▼
┌───────────────────────────── Retrieval (first hit wins) ─────────────────────────────┐
│ 1. Deterministic intents   file create, git, install, CLI shortcuts, project routing │
│ 2. Fuzzy KB match          RapidFuzz, score ≥ 90                                     │
│ 3. Semantic KB match       cosine similarity ≥ 0.45 on stored embeddings             │
│ 4. LLM fallback            Gemini generates commands as JSON                         │
└───────────────────────────────────────────────────────────────────────────────────────┘
   │
   ▼
Tool layer ── extract placeholders (<folder>, <filename>, …) ── missing? ask the user
   │
   ▼
Security validator ── unsafe? block
   │
   ▼
/resolve returns commands  ──►  user approves  ──►  /execute or /execute/stream
```

Embeddings come from the Gemini Embeddings API when `GEMINI_API_KEY` is set, and fall back to a local ONNX run of `all-MiniLM-L6-v2` (384-dim) otherwise. No vector database is used; embeddings are stored inline in the knowledge base JSON.

---

## Project Structure

```
.
├── clp-backend/
│   ├── server.py                 # FastAPI app and endpoints
│   ├── pipeline.py               # resolve(): chat guard → retrieve → tools → validate
│   ├── rebuild_embeddings.py     # Regenerate KB embeddings
│   ├── requirements.txt
│   ├── knowledge/
│   │   ├── knowledge_base.json   # 57 command rules (fs, git, npm, pip/python, sys, apps)
│   │   ├── retriever.py          # 4-tier retrieval
│   │   ├── semantic.py           # Cosine search + embedding rebuild
│   │   └── learner.py            # Persists successful LLM commands
│   ├── tools/
│   │   ├── tool_registry.py      # rule_id → Python-native handler
│   │   ├── tool_runner.py        # Runs tools / renders shell templates
│   │   ├── placeholder_extractor.py
│   │   ├── shell_renderer.py
│   │   └── app_launcher.py       # Allowlisted app launching
│   ├── filesystem/file_agent.py  # Create / copy / move / delete / find / search
│   ├── execution/                # Command executor and shell abstraction
│   ├── security/validator.py     # Deny-list and placeholder gate
│   ├── services/                 # Gemini LLM client and embeddings
│   ├── utils/                    # Normalizer, OS detector, project detector/router, chat classifier, history
│   ├── prompts/                  # LLM prompt templates
│   └── data/history.json         # Command history
├── termix-cli/
│   ├── electron/                 # main.cjs, preload.js
│   ├── src/
│   │   ├── App.jsx
│   │   ├── commands.js           # Built-in UI commands
│   │   ├── components/           # WelcomeScreen, TerminalUI, OutputLine
│   │   └── services/api.js       # Backend client
│   ├── index.html
│   └── vite.config.js
└── .agents/skills/               # Contributor guides for the KB and tool development
```

---

## Getting Started

### Prerequisites

- Python 3.10+ (developed on 3.13)
- Node.js 18+ and npm
- A [Google Gemini API key](https://aistudio.google.com/) (optional, but needed for the LLM fallback and `/explain`)

### 1. Start the backend

```bash
cd clp-backend
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`services/embedding.py` also imports `numpy`, `onnxruntime`, `huggingface_hub` and `transformers`, which are not listed in `requirements.txt`. If you see an import error, install them:

```bash
pip install numpy onnxruntime huggingface_hub transformers
```

Create `clp-backend/.env`:

```env
GEMINI_API_KEY=your_key_here
# Optional
GEMINI_MODEL=gemini-3-flash-preview
GEMINI_EMBEDDING_MODEL=gemini-embedding-001
GEMINI_EMBEDDING_TIMEOUT_MS=5000
CLP_AUTO_REBUILD_EMBEDDINGS=0
```

Run the server:

```bash
uvicorn server:app --reload --port 8000
```

Without a Gemini key, deterministic, fuzzy and semantic matching still work; only the LLM fallback and error explanation are unavailable.

### 2. Start the frontend

In a new terminal:

```bash
cd termix-cli
npm install
npm run dev                # browser at http://localhost:5173
# or
npm run electron:dev       # desktop app
```

The UI talks to `http://127.0.0.1:8000` by default. To point it elsewhere, set `VITE_API_BASE`.

### 3. Try it

```text
open terminal
create a folder hello
create a file hello.js
git status
install pandas
run tests
```

---

## Frontend Reference

**Built-in commands** (everything else goes to the backend):

| Command | Description |
|---|---|
| `help` | Show the command reference |
| `clear` | Clear the terminal |
| `version` | Show version info |
| `uptime` | Show session uptime |
| `theme <name>` | Switch theme: green, amber, blue, red, cyan |
| `history` | Show command history |
| `exit` | Exit the app |

**Shortcuts:** `↑` / `↓` history, `Ctrl+C` cancel, `Ctrl+L` clear, `Enter` run.

**Build a desktop installer:**

```bash
npm run electron:build     # outputs to dist-electron/ (dmg / nsis / AppImage)
```

---

## API Reference

Base URL: `http://127.0.0.1:8000` (interactive docs at `/docs`).

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/cwd` | Default starting directory (user home) |
| `POST` | `/resolve` | Phase 1: natural language → commands |
| `POST` | `/execute` | Phase 2: run approved commands, return results and new `cwd` |
| `POST` | `/execute/stream` | Same as `/execute` but streams output via SSE |
| `POST` | `/explain` | Explain a failed command and suggest a fix (needs Gemini key) |
| `POST` | `/input` | Send input to the session |
| `GET` | `/status` | Session status |

**`POST /resolve`**

```json
// request
{ "query": "create a folder demo", "session_id": "abc", "cwd": "/Users/me" }

// response
{
  "commands": ["mkdir demo"],
  "source": "kb_fuzzy",
  "source_display": "Knowledge Base (Fuzzy Match)",
  "score": 0.93,
  "intent": "create a new directory",
  "requires_confirmation": false,
  "missing_placeholders": [],
  "tool_output": "",
  "error": "",
  "cwd": "/Users/me"
}
```

**`/execute/stream` events:** plain output lines, `[CD] <path>`, `[ERROR] <msg>` and a final `[DONE] {"exit": 0, "cwd": "...", "success": true}`. Multiple commands can be chained with `|||`.

`source` is one of `intent`, `kb_fuzzy`, `kb_semantic`, `llm`, `file_ops` or `chat`.

---

## Extending the Knowledge Base

Rules live in `clp-backend/knowledge/knowledge_base.json`. Each rule has an `id`, `intent`, `examples`, per-OS `commands` (`linux`, `macos`, `windows`), `risk_level` and `requires_confirmation`. Use `<placeholder>` names in examples and commands.

After adding or editing rules, rebuild the stored embeddings:

```bash
cd clp-backend
python rebuild_embeddings.py
```

To add a Python-native tool, write a handler and register it in `tools/tool_registry.py`:

```python
TOOL_REGISTRY["rule_id"] = lambda args: my_handler(args.get("arg", ""))
```

Rules not in the registry fall through to the shell renderer, which fills the OS-specific template with the extracted placeholder values.

---

## Testing

```bash
cd clp-backend
python -m unittest test_project_router
```

---

## Safety Notes

- Commands run in a real shell on your machine with your permissions. Review each command before approving it.
- The validator blocks known-dangerous patterns, but it is a deny-list, not a sandbox.
- App launching is restricted to an allowlist (VS Code, Chrome, Safari, Firefox, Terminal, Finder, Calculator, Notepad, TextEdit).
- The backend enables CORS for all origins and the Electron window disables `webSecurity` for local API access. Both are fine for local development; tighten them before exposing the backend beyond `localhost`.
- Keep `.env` out of version control (it is already in `.gitignore`).

## Known Limitations

- Interactive scaffold flows (for example `create-react-app` prompts) are not fully implemented.
- Sessions are stored in memory and are lost when the server restarts.
- Windows support is template-based and less tested than macOS and Linux.

