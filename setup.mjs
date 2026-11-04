import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { HERE, HOMR_COMMIT, installed, ONLINE_PACKAGES, VENV, VENV_PYTHON, WINDOWS } from './python.mjs';

/**
 * npm run homr:setup: puts homr in its own Python environment
 * (tools/homr/.venv) and downloads its models. Uses the NVIDIA GPU when
 * there is one (`-- --cpu` to skip it). Needs Python 3.11 or 3.12, and git.
 */

const cpuOnly = process.argv.includes('--cpu') || spawnSync('nvidia-smi', ['-L'], { stdio: 'ignore' }).status !== 0;

function run(cmd, args) {
  console.log(`> ${cmd} ${args.join(' ')}`);
  const r = spawnSync(cmd, args, { stdio: 'inherit' });
  if (r.status !== 0) {
    console.error(r.error ? `${cmd} couldn’t be started (${r.error.message}).` : `That failed (exit code ${r.status}).`);
    process.exit(1);
  }
}

if (!installed()) {
  const python = WINDOWS ? ['py', ['-3.12']] : ['python3.12', []];
  const probe = spawnSync(python[0], [...python[1], '--version'], { encoding: 'utf8' });
  if (probe.status !== 0) {
    console.error(`Python 3.12 wasn’t found (${python[0]} ${python[1].join(' ')}). Install it from python.org, then run this again.`);
    process.exit(1);
  }
  run(python[0], [...python[1], '-m', 'venv', VENV]);
}

run(VENV_PYTHON, ['-m', 'pip', 'install', '--upgrade', 'pip']);
// onnxruntime (CPU) and onnxruntime-gpu can't be installed side by side.
run(VENV_PYTHON, ['-m', 'pip', 'uninstall', '-y', cpuOnly ? 'onnxruntime-gpu' : 'onnxruntime']);
run(VENV_PYTHON, ['-m', 'pip', 'install', `homr[${cpuOnly ? 'cpu' : 'cuda'}] @ git+https://github.com/liebharc/homr@${HOMR_COMMIT}`]);
// The online scanner's web server and Modal (npm run scanner:deploy), and iPhone photos (HEIC).
run(VENV_PYTHON, ['-m', 'pip', 'install', ...ONLINE_PACKAGES]);
run(VENV_PYTHON, [join(HERE, 'server.py'), '--init', ...(cpuOnly ? ['--cpu'] : [])]);

console.log('');
console.log(`The song scanner is set up (${cpuOnly ? 'CPU' : 'NVIDIA GPU'}). Start it with: npm run homr`);
