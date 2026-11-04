import { spawn, type ChildProcess } from 'node:child_process';
import { join } from 'node:path';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Connect, Plugin } from 'vite';
import { HERE, installed, VENV_PYTHON } from './python.mjs';

/**
 * The dev server starts the song scanner when a scan needs it. The app
 * calls POST /homr-launch when /homr doesn't answer; this starts
 * server.py (as `npm run homr` does) with --stop-when-idle, so it frees
 * the graphics card after 20 minutes without scans. Its messages show in
 * the dev server's window, marked [scanner]. A scanner already running
 * (started by hand) is left alone. It stops with the dev server.
 */

export const LAUNCH_PATH = '/homr-launch';
export const IDLE_MINUTES = 20;

/** What the app is told. */
export type LaunchState = 'running' | 'starting' | 'not-set-up' | 'failed';

/** What to do, from what's known: whether the scanner answers, is set up, and was already started here. */
export function launchDecision(s: { answers: boolean; installed: boolean; starting: boolean }): LaunchState | 'start' {
  if (s.answers) return 'running';
  if (s.starting) return 'starting';
  if (!s.installed) return 'not-set-up';
  return 'start';
}

// Kept across the dev server's own restarts (a changed config reloads this file), so a scan isn't cut off.
const kept = globalThis as { __fkHomr?: ChildProcess };

const alive = (child?: ChildProcess) => !!child && child.exitCode === null && child.signalCode === null;

async function answers(port: number): Promise<boolean> {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/health`, { signal: AbortSignal.timeout(1500) });
    return res.ok;
  } catch {
    return false;
  }
}

/** Each line the scanner prints, marked as its own. */
function relay(stream: NodeJS.ReadableStream | null, write: (line: string) => void) {
  let rest = '';
  stream?.setEncoding('utf8');
  stream?.on('data', (chunk: string) => {
    const lines = (rest + chunk).split(/\r?\n/);
    rest = lines.pop() ?? '';
    for (const line of lines) if (line.trim()) write(`[scanner] ${line}`);
  });
}

function start(port: number): ChildProcess {
  const child = spawn(VENV_PYTHON, ['-u', join(HERE, 'server.py'), '--port', String(port), '--stop-when-idle', String(IDLE_MINUTES)], {
    stdio: ['ignore', 'pipe', 'pipe'],
    // Piped, Python would print in the Windows code page and fail on a file name like "Für Elise".
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    windowsHide: true,
  });
  relay(child.stdout, (line) => console.log(line));
  relay(child.stderr, (line) => console.error(line));
  child.on('error', (e) => console.error(`[scanner] couldn’t be started: ${e.message}`));
  child.on('exit', (code) => {
    if (code) console.error(`[scanner] stopped (exit code ${code})`);
  });
  if (!kept.__fkHomr) process.on('exit', () => kept.__fkHomr?.kill());
  kept.__fkHomr = child;
  return child;
}

function send(res: ServerResponse, status: number, data: unknown) {
  res.statusCode = status;
  res.setHeader('Content-Type', 'application/json');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(data));
}

/** Only the app's own pages: the header, and no other website's Origin. */
function fromTheApp(req: IncomingMessage): boolean {
  if (!req.headers['x-fluent-keys']) return false;
  const origin = req.headers.origin;
  if (!origin) return true;
  try {
    return new URL(origin).host === req.headers.host;
  } catch {
    return false;
  }
}

function middleware(port: number): Connect.NextHandleFunction {
  return (req, res, next) => {
    if (req.url?.split('?')[0] !== LAUNCH_PATH || req.method !== 'POST') return next();
    if (!fromTheApp(req)) return send(res, 403, { state: 'failed', message: 'Only the app can start the scanner.' });
    void (async () => {
      const decision = launchDecision({ answers: await answers(port), installed: installed(), starting: alive(kept.__fkHomr) });
      if (decision !== 'start') return send(res, 200, { state: decision });
      console.log(`[scanner] starting, for a scan (it stops after ${IDLE_MINUTES} minutes without scans)`);
      const child = start(port);
      // A program that can't be run at all fails at once.
      await new Promise((resolve) => setTimeout(resolve, 300));
      send(res, 200, alive(child) || child.exitCode === 0 ? { state: 'starting' } : { state: 'failed' });
    })();
  };
}

export function homrLauncher(port: number): Plugin {
  return {
    name: 'homr-launcher',
    configureServer(server) {
      server.middlewares.use(middleware(port));
    },
    configurePreviewServer(server) {
      server.middlewares.use(middleware(port));
    },
  };
}
