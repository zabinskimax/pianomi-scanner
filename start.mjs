import { spawn } from 'node:child_process';
import { join } from 'node:path';
import { HERE, installed, VENV_PYTHON } from './python.mjs';

/**
 * npm run homr: starts the song scanner on this computer. Arguments after
 * `--` go to server.py (`npm run homr -- --cpu`, `--port 8766`, `--verbose`).
 */

if (!installed()) {
  console.log('The song scanner isn’t set up yet. Run this once (it downloads homr and its libraries):');
  console.log('');
  console.log('  npm run homr:setup');
  console.log('');
  console.log('See tools/homr/README.md.');
  process.exit(1);
}

const child = spawn(VENV_PYTHON, ['-u', join(HERE, 'server.py'), ...process.argv.slice(2)], { stdio: 'inherit' });
// Ctrl+C reaches the scanner too; wait for it to stop.
process.on('SIGINT', () => {});
child.on('exit', (code) => process.exit(code ?? 0));
