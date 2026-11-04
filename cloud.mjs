import { spawn, spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { HERE, installed, ONLINE_PACKAGES, VENV_PYTHON } from './python.mjs';

/**
 * The online song scanner (online.py), from the scanner's own Python
 * environment (npm run homr:setup):
 *
 *   npm run scanner:login     once: signs this computer in to Modal (opens the browser)
 *   npm run scanner:deploy    puts it on Modal (cloud.py) and prints its address
 *   npm run scanner:online    runs it here for testing, on http://127.0.0.1:8772
 *
 * The app and the site use the deployed address from src/songs/readers/online.ts
 * (ONLINE_SCANNER); on a dev server, localStorage['fk:scannerUrl'] points them
 * at the test copy instead.
 */

if (!installed()) {
  console.log('The song scanner isn’t set up yet. Run this once: npm run homr:setup');
  process.exit(1);
}

const has = spawnSync(VENV_PYTHON, ['-c', 'import fastapi, uvicorn, modal, pillow_heif'], { stdio: 'ignore' });
if (has.status !== 0) {
  console.log('> Installing what the online scanner needs…');
  const r = spawnSync(VENV_PYTHON, ['-m', 'pip', 'install', ...ONLINE_PACKAGES], { stdio: 'inherit' });
  if (r.status !== 0) process.exit(1);
}

const [what, ...rest] = process.argv.slice(2);
const commands = {
  login: ['-m', 'modal', 'token', 'new'],
  deploy: ['-m', 'modal', 'deploy', join(HERE, 'cloud.py')],
  local: ['-u', join(HERE, 'online.py'), '--cpu'],
};
if (!(what in commands)) {
  console.log('Use: node tools/homr/cloud.mjs login | deploy | local');
  process.exit(1);
}
// Modal prints the scanner's address (…modal.run) when it's deployed: put it in src/songs/readers/online.ts.
const child = spawn(VENV_PYTHON, [...commands[what], ...rest], { stdio: 'inherit', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
process.on('SIGINT', () => {});
child.on('exit', (code) => process.exit(code ?? 0));
