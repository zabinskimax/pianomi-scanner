import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { HERE, VENV } from './python.mjs';

/**
 * npm run scanner:publish: this folder, the online scanner's source, to its
 * public repository (homr is AGPL, so whoever runs it on a website shares
 * the program's code; the app and the site link it wherever a scan is asked
 * for). A snapshot of the files as they are, without the main repository's
 * history, committed and pushed to REPO. The repository must exist (an empty
 * public one, made on github.com); git asks to sign in the first time.
 */

const REPO = 'https://github.com/zabinskimax/pianomi-scanner.git';
const OUT = join(HERE, '.publish');
/** Not published: the Python environment, caches, and this script's own copy. */
const SKIP = new Set(['.venv', '__pycache__', '.publish', '.pytest_cache']);

function git(...args) {
  const r = spawnSync('git', args, { cwd: OUT, stdio: 'inherit' });
  if (r.status !== 0) process.exit(r.status ?? 1);
}

const quiet = (...args) => spawnSync('git', args, { cwd: OUT, encoding: 'utf8' }).stdout?.trim() ?? '';

if (!existsSync(join(OUT, '.git'))) {
  mkdirSync(OUT, { recursive: true });
  git('init', '-b', 'main');
  git('remote', 'add', 'origin', REPO);
}

// The snapshot replaces what was there.
for (const name of readdirSync(OUT)) if (name !== '.git') rmSync(join(OUT, name), { recursive: true, force: true });
for (const name of readdirSync(HERE)) {
  if (SKIP.has(name) || statSync(join(HERE, name)).isDirectory()) continue;
  copyFileSync(join(HERE, name), join(OUT, name));
}

// homr's licence text, from the homr installed here.
const site = join(VENV, process.platform === 'win32' ? 'Lib' : 'lib', 'site-packages');
const dist = readdirSync(site, { recursive: false }).find((d) => /^homr-.*\.dist-info$/.test(d));
const licence = dist && join(site, dist, 'licenses', 'LICENSE');
if (!licence || !existsSync(licence)) {
  console.error('homr’s LICENSE wasn’t found: run npm run homr:setup first.');
  process.exit(1);
}
copyFileSync(licence, join(OUT, 'LICENSE'));

const readme = readFileSync(join(OUT, 'README.md'), 'utf8');
writeFileSync(
  join(OUT, 'README.md'),
  `> This is the song scanner of [Pianomi](https://pianomi.app): [homr](https://github.com/liebharc/homr) with our additions, as it runs online for Pianomi's app and website. It's licensed under the AGPL-3.0, as homr is. Paths like \`src/…\` and \`docs/…\` refer to Pianomi's own repository, which isn't public.\n\n${readme}`,
);

const main = spawnSync('git', ['rev-parse', '--short', 'HEAD'], { cwd: HERE, encoding: 'utf8' }).stdout.trim();
git('add', '-A');
if (!quiet('status', '--porcelain')) {
  console.log('Nothing changed since the last publish.');
} else {
  git('commit', '-q', '-m', `The scanner as of Pianomi ${main}`);
}
git('push', '-u', 'origin', 'main');
console.log(`Published to ${REPO.replace(/\.git$/, '')}`);
