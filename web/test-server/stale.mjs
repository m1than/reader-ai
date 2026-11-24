// WEB-UI INV-UI-4 "Fresh builds": the [browser] tests run against the builds they test. A build is stale when it is
// older than the newest input: the app and story sources, the vendored library, the entry HTML, the package manifest,
// the lockfile and the Vite config. `.DS_Store` files are ignored (Finder writes them without changing a build).
// Live run 2026-11-24: a Storybook build from before the design pass failed two render.spec checks with no product
// fault.
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

export const INPUT_DIRS = ['src', '.storybook', 'vendor'];
export const INPUT_FILES = ['index.html', 'package.json', 'package-lock.json', 'vite.config.ts'];
export const BUILDS = ['dist/index.html', 'storybook-static/index.json'];

const mtime = (path) => {
  try {
    return statSync(path).mtimeMs;
  } catch {
    return null;
  }
};

const newestIn = (dir) => {
  let entries;
  try {
    entries = readdirSync(dir, { recursive: true, withFileTypes: true });
  } catch {
    return 0;
  }
  return entries
    .filter((d) => d.isFile() && d.name !== '.DS_Store')
    .reduce((m, d) => Math.max(m, mtime(join(d.parentPath, d.name)) ?? 0), 0);
};

/** The builds under `webRoot` that are missing or older than the newest input. */
export const staleBuilds = (webRoot) => {
  const inputs = Math.max(
    ...INPUT_DIRS.map((d) => newestIn(join(webRoot, d))),
    ...INPUT_FILES.map((f) => mtime(join(webRoot, f)) ?? 0),
  );
  return BUILDS.filter((b) => (mtime(join(webRoot, b)) ?? -1) < inputs);
};
