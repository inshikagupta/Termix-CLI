import React, { useState, useEffect, useRef, useCallback } from 'react';
import OutputLine from './OutputLine.jsx';
import { processCommand } from '../commands.js';
import {
  resolveQuery,
  executeCommands,
  executeStream,
  sendInteractiveInput,
  getInteractiveStatus,
  checkHealth,
  getDefaultCwd,
  explainError,
} from '../services/api.js';

function appendLines(setOutput, type, lines) {
  const id = Date.now() + Math.random();
  setOutput((prev) => [...prev, { type, lines, id }]);
}

/** Abbreviate a full path for the prompt: /Users/shubham/Desktop → ~/Desktop */
function shortenPath(fullPath) {
  if (!fullPath) return '~';
  const home = fullPath.match(/^\/Users\/[^/]+/) || fullPath.match(/^\/home\/[^/]+/);
  if (home) {
    return fullPath.replace(home[0], '~');
  }
  return fullPath;
}

const BOOT_LINES = [
  { type: 'system', lines: ['TermiX Shell v2.0.0  —  Electron + React + Vite'] },
  { type: 'system', lines: ['Copyright (c) 2025 TermiX. All rights reserved.'] },
  { type: 'system', lines: [''] },
  { type: 'system', lines: ['Initializing runtime...          [OK]'] },
  { type: 'system', lines: ['Loading AI modules...            [OK]'] },
];

export default function TerminalUI({ visible }) {
  const [output, setOutput] = useState([]);
  const [input, setInput] = useState('');
  const [cmdHistory, setCmdHistory] = useState([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [isProcessing, setIsProcessing] = useState(false);
  const [showContent, setShowContent] = useState(false);
  const [cwd, setCwd] = useState('~');                    // current working directory
  const [isStreaming, setIsStreaming] = useState(false);  // live output running
  const [isExplaining, setIsExplaining] = useState(false); // /explain call in flight
  const streamCancelRef = useRef(null);                   // cancel fn for active stream

  /** @type {'idle' | 'confirm' | 'running'} */
  const [flowStep, setFlowStep] = useState('idle');
  const [flowSessionId, setFlowSessionId] = useState(null);
  const [flowCommands, setFlowCommands] = useState([]);
  const [flowSource, setFlowSource] = useState(null);
  const [flowSourceDisplay, setFlowSourceDisplay] = useState(null);

  const resetInteractiveFlow = useCallback(() => {
    setFlowStep('idle');
    setFlowSessionId(null);
    setFlowCommands([]);
    setFlowSource(null);
    setFlowSourceDisplay(null);
  }, []);

  const inputRef = useRef(null);
  const bottomRef = useRef(null);
  const containerRef = useRef(null);

  // Animate boot lines + health check + fetch initial cwd
  useEffect(() => {
    if (!visible) return;
    const timer = setTimeout(() => setShowContent(true), 100);
    let i = 0;
    const interval = setInterval(() => {
      if (i < BOOT_LINES.length) {
        setOutput(prev => [...prev, { ...BOOT_LINES[i], id: Date.now() + i }]);
        i++;
      } else {
        clearInterval(interval);
        checkHealth()
          .then(async () => {
            appendLines(setOutput, 'success', ['Connecting to backend...         [OK]']);
            appendLines(setOutput, 'system', ['']);
            // Fetch the real starting cwd from the backend
            try {
              const initialCwd = await getDefaultCwd();
              if (initialCwd) setCwd(initialCwd);
            } catch (_) { /* non-fatal */ }
            appendLines(setOutput, 'info', ['Enter commands or natural language queries to execute. help / clear']);
            appendLines(setOutput, 'system', ['']);
          })
          .catch(() => {
            appendLines(setOutput, 'warn', ['Connecting to backend...         [OFFLINE]']);
            appendLines(setOutput, 'system', ['']);
            appendLines(setOutput, 'warn', ['⚠ Backend not running. Start with: cd clp-backend && uvicorn server:app --port 8000']);
            appendLines(setOutput, 'system', ['']);
            appendLines(setOutput, 'info', ['Enter commands or natural language queries to execute. help / clear']);
            appendLines(setOutput, 'system', ['']);
          });
        setTimeout(() => inputRef.current?.focus(), 100);
      }
    }, 60);
    return () => { clearTimeout(timer); clearInterval(interval); };
  }, [visible]);

  // Auto-scroll to bottom
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [output]);

  const focusInput = () => inputRef.current?.focus();

  const handleSubmit = useCallback(async () => {
    const trimmed = input.trim();
    setInput('');
    setHistoryIndex(-1);
    if (!trimmed) return;

    const cmdEntry = { type: 'command', text: trimmed, prompt: shortenPath(cwd), id: Date.now() };
    setOutput((prev) => [...prev, cmdEntry]);

    if (flowStep === 'idle' && trimmed.toLowerCase() !== 'clear' && trimmed.toLowerCase() !== 'help') {
      setCmdHistory((prev) => [trimmed, ...prev.filter((c) => c !== trimmed)].slice(0, 100));
    }

    setIsProcessing(true);

    // ── Built-in UI commands ────────────────────────────────────────────────
    if (trimmed.toLowerCase() === 'clear') {
      setTimeout(() => setOutput([]), 100);
      resetInteractiveFlow();
      setIsProcessing(false);
      return;
    }

    // ── Check built-in commands first (theme, version, uptime, etc.) ────────
    // Only in idle mode — not when waiting for y/n confirmation
    if (flowStep === 'idle') {
      try {
        const builtinResult = await processCommand(trimmed, cmdHistory);
        if (builtinResult !== null) {
          if (builtinResult.type === 'clear') {
            setTimeout(() => setOutput([]), 100);
          } else {
            appendLines(setOutput, builtinResult.type, builtinResult.lines || []);
          }
          setIsProcessing(false);
          return;
        }
      } catch (_) { /* fall through to backend */ }
    }

    try {
      // ── Confirmation step ──────────────────────────────────────────────────
      if (flowStep === 'confirm') {
        const t = trimmed.toLowerCase();
        if (t === 'no' || t === 'n') {
          appendLines(setOutput, 'system', ['Cancelled.']);
          resetInteractiveFlow();
          return;
        }
        if (t !== 'yes' && t !== 'y') {
          appendLines(setOutput, 'info', ['Reply with yes or no (or y/n).']);
          return;
        }

        const sid = flowSessionId;
        const cmds = flowCommands;
        const src = flowSource;

        // Use streaming for confirmed commands
        setIsStreaming(true);
        resetInteractiveFlow();

        const liveLineId = Date.now() + Math.random();
        setOutput(prev => [...prev, { type: 'system', lines: [], id: liveLineId, streaming: true }]);

        const cancel = executeStream(sid, cmds, src, cwd, {
          onLine: (line) => {
            setOutput(prev => prev.map(entry =>
              entry.id === liveLineId
                ? { ...entry, lines: [...entry.lines, line] }
                : entry
            ));
          },
          onDone: ({ cwd: newCwd, success, cdOnly }) => {
            setIsStreaming(false);
            streamCancelRef.current = null;
            if (newCwd) setCwd(newCwd);
            // Seal the streaming block
            setOutput(prev => prev.map(entry =>
              entry.id === liveLineId
                ? { ...entry, streaming: false, type: success ? 'success' : 'error' }
                : entry
            ));
          },
          onError: (msg) => {
            setIsStreaming(false);
            streamCancelRef.current = null;
            appendLines(setOutput, 'error', [`Error: ${msg}`]);
          },
        });
        streamCancelRef.current = cancel;
        return;
      }

      // ── Running step (interactive stdin) ──────────────────────────────────
      if (flowStep === 'running') {
        if (/^ask\s/i.test(trimmed)) {
          appendLines(setOutput, 'error', ['Cannot start a new ask while a process is running.']);
          return;
        }
        const sid = flowSessionId;
        const res = await sendInteractiveInput(sid, trimmed);
        if (res.line != null && res.line !== '') {
          appendLines(setOutput, 'system', [res.line]);
        }
        const st = await getInteractiveStatus(sid);
        if (st.status === 'completed') {
          appendLines(setOutput, 'success', ['Command completed.']);
          resetInteractiveFlow();
        }
        return;
      }

      // ── New query / command ──────────────────────────────────────────────
      const query = trimmed;
      resetInteractiveFlow();
      const sid = crypto.randomUUID();
      setFlowSessionId(sid);

      const r = await resolveQuery(query, sid, cwd);

      // Update cwd from resolve response (in case pipeline changed it)
      if (r.cwd) setCwd(r.cwd);

      if (r.error) {
        appendLines(setOutput, 'error', [`Blocked: ${r.error}`]);
        resetInteractiveFlow();
        return;
      }

      if (r.missing_placeholders && r.missing_placeholders.length > 0) {
        appendLines(setOutput, 'info', [
          'Need more information:',
          ...r.missing_placeholders.map(p => `  • ${p}`),
          '',
          'Please provide the missing value(s):',
        ]);
        setFlowStep('idle');
        return;
      }

      // Python-native tool already executed
      if (r.tool_output && r.tool_output.trim()) {
        appendLines(setOutput, 'success', [
          `[Source: ${r.source_display}]`,
          r.tool_output.trimEnd(),
        ]);
        resetInteractiveFlow();
        return;
      }

      // Commands — stream them immediately without asking for confirmation
      // UNLESS requires_confirmation is set (for destructive ops)
      if (r.commands && r.commands.length > 0) {
        if (r.requires_confirmation) {
          // Show confirmation prompt for destructive operations
          setFlowCommands(r.commands);
          setFlowSource(r.source);
          setFlowSourceDisplay(r.source_display);
          setFlowStep('confirm');

          const outputLines = ['⚠ This operation requires confirmation:'];
          r.commands.forEach((cmd, i) => {
            outputLines.push(`  ${i + 1}. ${cmd}`);
          });
          outputLines.push('');
          outputLines.push(`[Source: ${r.source_display}]`);
          outputLines.push('');
          outputLines.push('Execute these command(s)? [y/N]:');
          appendLines(setOutput, 'warn', outputLines);
          return;
        }

        // Auto-execute non-destructive commands with streaming
        setIsStreaming(true);
        const liveLineId = Date.now() + Math.random();
        setOutput(prev => [...prev, { type: 'system', lines: [`[Source: ${r.source_display}]`], id: liveLineId, streaming: true }]);

        const cancel = executeStream(sid, r.commands, r.source, cwd, {
          onLine: (line) => {
            setOutput(prev => prev.map(entry =>
              entry.id === liveLineId
                ? { ...entry, lines: [...entry.lines, line] }
                : entry
            ));
          },
          onDone: ({ cwd: newCwd, success }) => {
            setIsStreaming(false);
            streamCancelRef.current = null;
            if (newCwd) setCwd(newCwd);
            setOutput(prev => prev.map(entry =>
              entry.id === liveLineId
                ? { ...entry, streaming: false, type: success ? 'success' : 'error' }
                : entry
            ));
            if (!success) {
              // Offer error explanation
              const capturedLines = [];
              setOutput(prev => {
                const block = prev.find(e => e.id === liveLineId);
                if (block) capturedLines.push(...(block.lines || []));
                return [
                  ...prev,
                  {
                    type: 'explain_prompt',
                    command: r.commands.join(' && '),
                    outputText: capturedLines.join('\n'),
                    cwd,
                    id: Date.now() + Math.random(),
                  },
                ];
              });
            }
            resetInteractiveFlow();
          },
          onError: (msg) => {
            setIsStreaming(false);
            streamCancelRef.current = null;
            appendLines(setOutput, 'error', [`Error: ${msg}`]);
            resetInteractiveFlow();
          },
        });
        streamCancelRef.current = cancel;
        return;
      }

      appendLines(setOutput, 'error', ['No commands generated.']);
      resetInteractiveFlow();

    } catch (err) {
      resetInteractiveFlow();
      appendLines(setOutput, 'error', [`Error: ${err.message}`]);
    } finally {
      setIsProcessing(false);
    }
  }, [input, flowStep, flowSessionId, flowCommands, flowSource, flowSourceDisplay, cwd, resetInteractiveFlow]);

  const handleExplain = useCallback(async (entry) => {
    setIsExplaining(true);
    // Replace the explain_prompt with a loading indicator
    setOutput(prev => prev.map(e =>
      e.id === entry.id ? { ...e, loading: true } : e
    ));
    try {
      const result = await explainError(entry.command, entry.outputText, '', entry.cwd);
      setOutput(prev => prev.map(e =>
        e.id === entry.id
          ? {
              ...e,
              loading: false,
              type: 'explanation',
              lines: [
                '╔══ 🤖 AI Diagnosis ═══════════════════════════════════╗',
                ...(result.explanation || 'No explanation available.').split('\n').map(l => `  ${l}`),
                ...(result.suggested_fix ? [
                  '',
                  '  ✦ Suggested fix:',
                  `  ${result.suggested_fix}`,
                ] : []),
                '╚══════════════════════════════════════════════════════╝',
              ],
            }
          : e
      ));
    } catch (err) {
      setOutput(prev => prev.map(e =>
        e.id === entry.id
          ? { ...e, loading: false, type: 'error', lines: [`Failed to get explanation: ${err.message}`] }
          : e
      ));
    } finally {
      setIsExplaining(false);
    }
  }, []);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      handleSubmit();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      const newIdx = Math.min(historyIndex + 1, cmdHistory.length - 1);
      setHistoryIndex(newIdx);
      setInput(cmdHistory[newIdx] || '');
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      const newIdx = Math.max(historyIndex - 1, -1);
      setHistoryIndex(newIdx);
      setInput(newIdx === -1 ? '' : cmdHistory[newIdx]);
    } else if (e.key === 'c' && e.ctrlKey) {
      // Cancel active stream
      if (streamCancelRef.current) {
        streamCancelRef.current();
        streamCancelRef.current = null;
        setIsStreaming(false);
      }
      setInput('');
      resetInteractiveFlow();
      setOutput((prev) => [...prev, { type: 'system', lines: ['^C'], id: Date.now() }]);
    } else if (e.key === 'l' && e.ctrlKey) {
      e.preventDefault();
      setOutput([]);
      resetInteractiveFlow();
    }
  };

  if (!visible) return null;

  const shortCwd = shortenPath(cwd);
  const promptText =
    flowStep === 'confirm' ? '[y/N]>' :
    flowStep === 'running' ? 'stdin>' :
    `${shortCwd}$`;

  const statusLabel =
    isStreaming ? '▶ RUNNING' :
    isProcessing ? '⟳ PROCESSING' :
    flowStep === 'confirm' ? '? CONFIRM' :
    '● READY';

  return (
    <div
      onClick={focusInput}
      style={{
        position: 'fixed', inset: 0,
        background: 'var(--bg-primary)',
        display: 'flex', flexDirection: 'column',
        opacity: showContent ? 1 : 0,
        transition: 'opacity 0.4s ease',
        cursor: 'text',
      }}
    >
      {/* Scanline */}
      <div style={{
        position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 100,
        background: 'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.015) 2px, rgba(0,0,0,0.015) 4px)',
      }} />

      {/* Top title bar */}
      <div style={{
        height: 38,
        background: 'var(--bg-secondary)',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center',
        padding: '0 16px',
        flexShrink: 0,
        WebkitAppRegion: 'drag',
        gap: 8,
      }}>
        <div style={{
          flex: 1, textAlign: 'center',
          fontSize: 12, color: 'rgba(0,255,156,0.4)',
          letterSpacing: 3, textTransform: 'uppercase',
          fontFamily: 'var(--font-mono)',
        }}>
          TermiX CLI — {shortCwd}
        </div>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', WebkitAppRegion: 'no-drag' }}>
          <span style={{ fontSize: 10, color: 'rgba(0,255,156,0.3)', letterSpacing: 1 }}>
            {statusLabel}
          </span>
        </div>
      </div>

      {/* Tab bar */}
      <div style={{
        height: 30,
        background: 'var(--bg-secondary)',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: 0,
        flexShrink: 0,
      }}>
        <div style={{
          padding: '0 20px',
          height: '100%',
          display: 'flex', alignItems: 'center',
          background: 'var(--bg-primary)',
          borderRight: '1px solid var(--border)',
          fontSize: 11, color: 'var(--green)',
          letterSpacing: 1,
          gap: 6,
        }}>
          <span style={{ fontSize: 9, opacity: 0.6 }}>⬤</span>
          termix
        </div>
        <div style={{
          padding: '0 16px', height: '100%',
          display: 'flex', alignItems: 'center',
          fontSize: 11, color: 'var(--muted-text)',
          letterSpacing: 1, gap: 6, cursor: 'pointer',
          opacity: 0.4,
        }}>
          <span style={{ fontSize: 12 }}>+</span>
        </div>
      </div>

      {/* Output area */}
      <div
        ref={containerRef}
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '16px 20px 8px',
          fontFamily: 'var(--font-mono)',
          fontSize: 13,
          lineHeight: 1.6,
        }}
      >
        {output.map((entry, i) => {
          // ── Explain prompt: inline button after a failed command ──
          if (entry.type === 'explain_prompt') {
            return (
              <div key={entry.id || i} style={{ marginBottom: 8, paddingLeft: 4 }}>
                {entry.loading ? (
                  <span style={{ color: 'rgba(0,255,156,0.5)', fontSize: 12, fontFamily: 'var(--font-mono)' }}>
                    🤖 Diagnosing error...
                  </span>
                ) : (
                  <button
                    onClick={() => handleExplain(entry)}
                    disabled={isExplaining}
                    style={{
                      background: 'transparent',
                      border: '1px solid rgba(0,255,156,0.25)',
                      borderRadius: 4,
                      color: 'rgba(0,255,156,0.6)',
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                      padding: '3px 10px',
                      cursor: isExplaining ? 'default' : 'pointer',
                      letterSpacing: '0.05em',
                      transition: 'all 0.2s',
                    }}
                    onMouseEnter={e => { e.target.style.borderColor = 'rgba(0,255,156,0.6)'; e.target.style.color = 'var(--green)'; }}
                    onMouseLeave={e => { e.target.style.borderColor = 'rgba(0,255,156,0.25)'; e.target.style.color = 'rgba(0,255,156,0.6)'; }}
                  >
                    🤖 Why did this fail? →
                  </button>
                )}
              </div>
            );
          }

          // ── Explanation block ─────────────────────────────────────
          if (entry.type === 'explanation') {
            return (
              <div key={entry.id || i} style={{ marginBottom: 10 }}>
                {(entry.lines || []).map((line, li) => (
                  <div key={li} style={{
                    color: line.includes('✦') ? 'var(--amber)' : 'rgba(0,255,156,0.7)',
                    paddingLeft: 4, lineHeight: '1.7',
                    whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)', fontSize: 13,
                  }}>
                    {line || '\u00A0'}
                  </div>
                ))}
              </div>
            );
          }

          return <OutputLine key={entry.id || i} entry={entry} index={i} />;
        })}

        {/* Streaming indicator */}
        {isStreaming && (
          <div style={{
            color: 'rgba(0,255,156,0.5)',
            fontSize: 12,
            paddingLeft: 4,
            animation: 'blink 0.8s step-end infinite',
          }}>
            ▶ streaming output... (Ctrl+C to cancel)
          </div>
        )}

        {/* Processing indicator */}
        {isProcessing && !isStreaming && (
          <div style={{
            color: 'rgba(0,255,156,0.5)',
            fontSize: 12,
            paddingLeft: 4,
            animation: 'blink 0.8s step-end infinite',
          }}>
            ⟳ processing...
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input row */}
      <div style={{
        padding: '10px 20px 14px',
        borderTop: '1px solid var(--border)',
        background: 'var(--bg-secondary)',
        flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {/* Prompt — shows real path */}
          <span style={{
            color: flowStep === 'confirm' ? 'var(--amber)' :
                   isStreaming ? 'rgba(0,255,156,0.3)' :
                   'rgba(0,255,156,0.6)',
            fontSize: 13,
            fontFamily: 'var(--font-mono)',
            flexShrink: 0,
            userSelect: 'none',
            whiteSpace: 'nowrap',
          }}>
            {promptText}
          </span>

          {/* Input */}
          <input
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isProcessing && !isStreaming}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            style={{
              flex: 1,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--green)',
              fontFamily: 'var(--font-mono)',
              fontSize: 13,
              caretColor: 'var(--green)',
              padding: 0,
              userSelect: 'text',
            }}
          />
        </div>

        {/* Bottom hints */}
        <div style={{
          marginTop: 6,
          display: 'flex', gap: 20,
          fontSize: 10, color: 'var(--muted-text)',
          letterSpacing: '0.05em',
        }}>
          <span>↑↓ history</span>
          <span>Ctrl+C cancel</span>
          <span>Ctrl+L clear</span>
          <span style={{ marginLeft: 'auto', color: 'rgba(0,255,156,0.2)' }}>
            {cmdHistory.length} commands
          </span>
        </div>
      </div>
    </div>
  );
}
