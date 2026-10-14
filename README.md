# Terminal Chatbot

Minimal run guide for the current project.

## What It Is

- `clp-backend/` — Python backend for NL-to-command resolution
- `termix-cli/` — React/Electron terminal-style frontend

## 1. Run The Backend

```bash
cd /Users/Shubham/SEM4/terminal_chatbot/clp-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

If you want LLM fallback, create `clp-backend/.env` and add:

```env
GEMINI_API_KEY=your_key_here
```

## 2. Run The Frontend

Open a new terminal:

```bash
cd /Users/Shubham/SEM4/terminal_chatbot/termix-cli
npm install
npm run dev
```

For Electron:

```bash
npm run electron:dev
```

## 3. Use It

Make sure the backend is running on `http://127.0.0.1:8000`, then try:

```text
open terminal
create a folder hello
create a file hello.js
git status
```

## Notes

- Some developer workflows are deterministic and handled locally.
- LLM fallback only works when `GEMINI_API_KEY` is set.
- Interactive scaffold flows are not fully implemented yet.
