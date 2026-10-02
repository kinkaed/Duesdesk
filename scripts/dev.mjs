// Runs the Django backend and the Vite dev server together, so `pnpm dev` gives a
// working app with one command.
//
// The two have to agree on a port: serve.py reads $PORT and defaults to 10000,
// while the Vite proxy targets 8765. Running them separately and forgetting
// PORT left them unable to see each other, which surfaced as the API failing
// while the page loaded. The port comes from dev.config.mjs, which vite.config.ts
// imports too, so there is only one place to change it.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { BACKEND_ORIGIN, BACKEND_PORT } from '../dev.config.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// Prefer the project virtualenv, then whatever python is on PATH. Serving with a
// different interpreter than the one holding the dependencies fails in ways that
// look like application errors, so say which one was chosen.
const venv = process.platform === 'win32'
  ? join(root, '.venv', 'Scripts', 'python.exe')
  : join(root, '.venv', 'bin', 'python');
const python = existsSync(venv) ? venv : (process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3'));

const children = [];
function run(name, command, args, env) {
  const child = spawn(command, args, {
    cwd: root,
    env: { ...process.env, ...env },
    stdio: ['ignore', 'inherit', 'inherit'],
    // No shell: the venv path and the arguments are passed through as-is, so a
    // project path containing spaces cannot turn into two arguments.
    shell: false,
  });
  child.on('exit', (code, signal) => {
    if (!stopping) {
      console.error(`\n[dev] ${name} exited (${signal || code}); stopping the other process.`);
      shutdown(typeof code === 'number' ? code : 1);
    }
  });
  children.push(child);
  return child;
}

let stopping = false;
function shutdown(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (child.exitCode === null && !child.killed) {
      // Vite and Waitress both handle SIGTERM; Waitress needs it to close its
      // listening socket rather than leaving the port bound.
      try { child.kill('SIGTERM'); } catch { /* already gone */ }
    }
  }
  setTimeout(() => {
    for (const child of children) {
      if (child.exitCode === null) { try { child.kill('SIGKILL'); } catch { /* already gone */ } }
    }
    process.exit(code);
  }, 1500).unref();
}

for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => shutdown(0));
process.on('exit', () => { if (!stopping) shutdown(0); });

console.log(`[dev] backend: ${python} (PORT=${BACKEND_PORT})`);
console.log(`[dev] frontend: http://127.0.0.1:5173  ->  ${BACKEND_ORIGIN}`);
run('backend', python, ['backend/serve.py'], { PORT: String(BACKEND_PORT) });

// Wait for the backend before Vite starts proxying to it, so the first page load
// is not a race against a socket that is not listening yet.
for (let attempt = 0; attempt < 60; attempt++) {
  if (stopping) break;
  try {
    const response = await fetch(`${BACKEND_ORIGIN}/health/`);
    if (response.ok) {
      console.log('[dev] backend is up');
      break;
    }
  } catch { /* not listening yet */ }
  if (attempt === 59) console.error(`[dev] backend did not answer on ${BACKEND_ORIGIN}; starting Vite anyway.`);
  await sleep(250);
}

// Vite is run through its own bin script with the current interpreter rather than
// through `pnpm exec`. A .cmd shim cannot be spawned on Windows with shell:false
// and fails with EINVAL, and going direct also means the dev server does not
// depend on the package manager being on PATH.
const viteBin = join(root, 'node_modules', 'vite', 'bin', 'vite.js');
if (!existsSync(viteBin)) {
  console.error(`[dev] ${viteBin} is missing. Run pnpm install first.`);
  shutdown(1);
} else {
  run('frontend', process.execPath, [viteBin, '--host', '127.0.0.1'], {});
}
