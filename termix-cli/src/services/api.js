const DEFAULT_BASE = 'http://127.0.0.1:8000';

function baseUrl() {
  return (import.meta.env.VITE_API_BASE || DEFAULT_BASE).replace(/\/$/, '');
}

async function parseJsonResponse(res) {
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(
      res.ok
        ? `Invalid JSON from server: ${text.slice(0, 200)}`
        : `HTTP ${res.status}: ${text.slice(0, 200)}`
    );
  }

  if (!res.ok) {
    const msg =
      data.detail != null
        ? typeof data.detail === 'string'
          ? data.detail
          : JSON.stringify(data.detail)
        : data.message || data.error || `Request failed (${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    err.data = data;
    throw err;
  }

  return data;
}

/**
 * GET /cwd — fetch the initial working directory (user home) on boot.
 */
export async function getDefaultCwd() {
  const res = await fetch(`${baseUrl()}/cwd`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(3000),
  });
  const data = await parseJsonResponse(res);
  return data.cwd || '';
}

/**
 * POST /resolve — resolve a natural language query into commands.
 * Returns: { commands, source, source_display, intent, error?, missing_placeholders?, tool_output?, cwd }
 */
export async function resolveQuery(query, session_id = '', cwd = '') {
  const res = await fetch(`${baseUrl()}/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ query, session_id, cwd }),
  });
  return parseJsonResponse(res);
}

/**
 * POST /execute — execute approved commands (non-streaming).
 * Returns: { results, line, cwd }
 */
export async function executeCommands(session_id, commands, source, cwd = '') {
  const commandStr = Array.isArray(commands) ? commands.join('|||') : commands;
  const res = await fetch(`${baseUrl()}/execute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ session_id, command: commandStr, source, cwd }),
  });
  return parseJsonResponse(res);
}

/**
 * POST /execute/stream — execute a command with live streaming output.
 *
 * Calls onLine(line: string) for each output line received.
 * Calls onDone({ exit: number, cwd: string, success: boolean }) when complete.
 * Calls onError(message: string) on validation / network error.
 *
 * Returns a cancel function: call it to abort the stream.
 */
export function executeStream(session_id, commands, source, cwd = '', { onLine, onDone, onError } = {}) {
  const commandStr = Array.isArray(commands) ? commands.join('|||') : commands;
  const controller = new AbortController();

  (async () => {
    try {
      const res = await fetch(`${baseUrl()}/execute/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify({ session_id, command: commandStr, source, cwd }),
        signal: controller.signal,
      });

      if (!res.ok) {
        const text = await res.text();
        onError?.(text || `HTTP ${res.status}`);
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE events are separated by double newlines
        const parts = buffer.split('\n\n');
        buffer = parts.pop(); // last incomplete chunk

        for (const part of parts) {
          const line = part.replace(/^data:\s?/, '');
          if (!line) continue;

          if (line.startsWith('[DONE]')) {
            try {
              const payload = JSON.parse(line.slice('[DONE]'.length).trim());
              onDone?.(payload);
            } catch {
              onDone?.({ exit: 0, cwd, success: true });
            }
          } else if (line.startsWith('[ERROR]')) {
            onError?.(line.slice('[ERROR]'.length).trim());
          } else if (line.startsWith('[CD]')) {
            const newCwd = line.slice('[CD]'.length).trim();
            onLine?.(`→ Changed directory to: ${newCwd}`);
            onDone?.({ exit: 0, cwd: newCwd, success: true, cdOnly: true });
          } else {
            onLine?.(line);
          }
        }
      }
    } catch (err) {
      if (err.name !== 'AbortError') {
        onError?.(err.message);
      }
    }
  })();

  return () => controller.abort();
}

/** POST /input */
export async function sendInteractiveInput(session_id, input) {
  const res = await fetch(`${baseUrl()}/input`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ session_id, input }),
  });
  return parseJsonResponse(res);
}

/** GET /status */
export async function getInteractiveStatus(session_id) {
  const q = new URLSearchParams({ session_id });
  const res = await fetch(`${baseUrl()}/status?${q}`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
  return parseJsonResponse(res);
}

/** GET /health */
export async function checkHealth() {
  const res = await fetch(`${baseUrl()}/health`, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(3000),
  });
  return parseJsonResponse(res);
}

/**
 * POST /explain — ask the LLM why a command failed and how to fix it.
 * Returns: { explanation: string, suggested_fix: string }
 */
export async function explainError(command, output, error, cwd = '') {
  const res = await fetch(`${baseUrl()}/explain`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ command, output, error, cwd }),
  });
  return parseJsonResponse(res);
}
