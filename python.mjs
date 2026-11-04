import { existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/** Where the song scanner's Python environment lives (tools/homr/.venv). */

export const HERE = dirname(fileURLToPath(import.meta.url));
export const VENV = join(HERE, '.venv');
export const WINDOWS = process.platform === 'win32';
export const VENV_PYTHON = WINDOWS ? join(VENV, 'Scripts', 'python.exe') : join(VENV, 'bin', 'python');

/** homr from GitHub at a fixed commit (newer than its last release, with better photo reading). */
export const HOMR_COMMIT = 'ab369ac54277afab582afa4725441f7cbba76593';

/** What the online scanner (online.py, cloud.py) needs besides homr; npm run homr:setup installs them too. */
export const ONLINE_PACKAGES = ['fastapi>=0.115,<1', 'uvicorn>=0.32,<1', 'httpx>=0.27,<1', 'modal>=1.0,<2', 'pillow-heif>=0.18,<2'];

export const installed = () => existsSync(VENV_PYTHON);
