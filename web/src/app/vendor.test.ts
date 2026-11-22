// @trace FR-UI-1
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { webRoot } from './testing/fixtures';
import { load } from './testing/load';

const lock = () =>
  JSON.parse(readFileSync(`${webRoot}vendor/acme-ui.lock.json`, 'utf8')) as Record<string, string | null>;

describe('vendored library (FR-UI-1)', () => {
  it('pins tag v0.1.0 to a full commit and records the tools used', () => {
    const l = lock();
    expect(l.tag).toBe('v0.1.0');
    expect(l.commit).toMatch(/^[0-9a-f]{40}$/);
    expect(l.tarball).toBe('acme-ui-v0.1.0.tgz');
    expect(l.node).toMatch(/^v\d+\.\d+\.\d+$/);
    expect(l.npm).toMatch(/^\d+\.\d+\.\d+$/);
  });

  it('the committed tarball matches the recorded sha256', () => {
    const l = lock();
    const bytes = readFileSync(`${webRoot}vendor/${l.tarball}`);
    expect(createHash('sha256').update(bytes).digest('hex')).toBe(l.sha256);
  });

  it('package.json depends on the tarball by file path', () => {
    const pkg = JSON.parse(readFileSync(`${webRoot}package.json`, 'utf8')) as { dependencies: Record<string, string> };
    expect(pkg.dependencies['@acme/ui']).toBe('file:vendor/acme-ui-v0.1.0.tgz');
  });

  it('the vendor script checks tag → commit and never writes to a remote', () => {
    const script = readFileSync(`${webRoot}scripts/vendor-ui.mjs`, 'utf8');
    expect(script).toMatch(/'rev-parse', `\$\{lock\.tag\}\^\{commit\}`/);
    expect(script).not.toMatch(/\[\s*'(push|tag|remote)'/);
    expect(script).not.toMatch(/'publish'/);
  });

  it('imports only the allowed library entries', async () => {
    const { allowedEntries, libraryImports } = await load(() => import('./sourceScan'));
    expect(allowedEntries).toBeInstanceOf(Array);
    const outside = libraryImports().filter((e) => !allowedEntries.includes(e));
    expect(outside).toEqual([]);
  });
});
