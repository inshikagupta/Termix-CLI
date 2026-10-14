/**
 * commands.js — TermiX built-in UI commands.
 *
 * Only pure UI commands live here (clear, help, version, uptime, theme, exit, history).
 * Everything else (ls, pwd, cd, whoami, echo, ping, npm, etc.) goes through the
 * backend AI pipeline and runs as real shell commands.
 */

const BOOT_TIME = new Date();

const commands = {
  help: () => ({
    type: 'info',
    lines: [
      '╔══════════════════════════════════════════════════╗',
      '║           TermiX CLI — Command Reference         ║',
      '╚══════════════════════════════════════════════════╝',
      '',
      '  BUILT-IN',
      '  ──────────────────────────────────────────────',
      '  help          Show this help menu',
      '  clear         Clear terminal history',
      '  version       Show TermiX version info',
      '  uptime        Show session uptime',
      '  theme <name>  Switch color theme (green/amber/blue/red/cyan)',
      '  history       Show command history',
      '  exit          Exit TermiX',
      '',
      '  SHELL / AI (all other input)',
      '  ──────────────────────────────────────────────',
      '  Any shell command or natural language query is sent',
      '  to the backend and executed in your current directory.',
      '',
      '  Examples:',
      '  ls -la               List files (real)',
      '  pwd                  Show real working directory',
      '  cd ~/Desktop         Change to Desktop',
      '  create file hello.txt  Natural language → touch hello.txt',
      '  install pandas         Natural language → pip install pandas',
      '  npm run dev            Streams live output as it runs',
      '',
      '  TIP: Use ↑↓ arrow keys to navigate command history',
      '  TIP: Ctrl+C cancels a running command',
      '  TIP: Ctrl+L clears the screen',
    ],
  }),

  clear: () => ({ type: 'clear' }),

  version: () => ({
    type: 'success',
    lines: [
      'TermiX CLI v2.0.0',
      'Runtime: Electron + React + Vite + FastAPI',
      'Features: streaming output, real cwd, AI pipeline',
    ],
  }),

  uptime: () => {
    const now = new Date();
    const diff = Math.floor((now - BOOT_TIME) / 1000);
    const h = Math.floor(diff / 3600).toString().padStart(2, '0');
    const m = Math.floor((diff % 3600) / 60).toString().padStart(2, '0');
    const s = (diff % 60).toString().padStart(2, '0');
    return {
      type: 'info',
      lines: [`Session uptime: ${h}:${m}:${s}`, `Started: ${BOOT_TIME.toLocaleTimeString()}`],
    };
  },

  history: (args, history) => ({
    type: 'info',
    lines: history.length
      ? history.map((cmd, i) => `  ${String(i + 1).padStart(3)}  ${cmd}`)
      : ['  (no history yet)'],
  }),

  theme: (args) => {
    const themes = { green: '#00FF9C', amber: '#FFB800', blue: '#58A6FF', red: '#FF4545', cyan: '#00D4FF' };
    const name = args[0]?.toLowerCase();
    if (!name || !themes[name]) {
      return { type: 'info', lines: [`Available themes: ${Object.keys(themes).join(', ')}`] };
    }
    document.documentElement.style.setProperty('--green', themes[name]);
    document.documentElement.style.setProperty('--green-dim', themes[name] + 'CC');
    document.documentElement.style.setProperty('--green-glow', themes[name] + '26');
    document.documentElement.style.setProperty('--green-glow-strong', themes[name] + '66');
    return { type: 'success', lines: [`Theme switched to: ${name}`, `Color: ${themes[name]}`] };
  },

  exit: () => {
    setTimeout(() => window.close?.(), 800);
    return { type: 'warn', lines: ['Goodbye. Closing TermiX...'] };
  },
};

/**
 * processCommand — handle input that matches a built-in command.
 * Returns null if the input is NOT a built-in (caller should send to backend).
 */
export async function processCommand(input, commandHistory) {
  const trimmed = input.trim();
  if (!trimmed) return null;

  const parts = trimmed.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const args = parts.slice(1);

  if (cmd in commands) {
    const handler = commands[cmd];
    const result = await handler(args, commandHistory);
    return result;
  }

  // Not a built-in — return null so TerminalUI sends it to the backend
  return null;
}
