// @trace INV-UI-2 FR-UI-8
import { readFileSync } from 'node:fs';
import { cleanup, render } from '@testing-library/react';
import type * as React from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { webRoot } from './testing/fixtures';
import { load } from './testing/load';

afterEach(cleanup);

describe('no HTML strings in the app document (INV-UI-2)', () => {
  it('no source file uses an HTML-string or eval API', async () => {
    const { forbiddenUses } = await load(() => import('./sourceScan'));
    expect(forbiddenUses).toBeTypeOf('function');
    expect(forbiddenUses()).toEqual([]);
  });

  it('the scanner itself catches each forbidden API', async () => {
    const { findForbidden } = await load(() => import('./sourceScan'));
    expect(findForbidden).toBeTypeOf('function');
    for (const snippet of [
      '<div dangerouslySetInnerHTML={{__html: x}} />', 'el.innerHTML = x', 'el.outerHTML = x',
      'el.insertAdjacentHTML("a", x)', 'document.write(x)', 'new DOMParser()', 'r.createContextualFragment(x)',
      'eval(x)', 'new Function(x)', "setTimeout('x()', 1)", 'setInterval("x()", 1)',
    ]) expect(findForbidden(snippet)).not.toEqual([]);
    expect(findForbidden('const a = 1;')).toEqual([]);
  });

  it('the build resolves DOM-parsing dependency builds to their JavaScript builds', () => {
    const config = readFileSync(`${webRoot}vite.config.ts`, 'utf8');
    expect(config).toContain("find: /^hast-util-from-html-isomorphic$/");
    expect(config).toContain('./node_modules/hast-util-from-html-isomorphic/index.js');
    expect(config).toContain("find: /^decode-named-character-reference$/");
    expect(config).toContain('./node_modules/decode-named-character-reference/index.js');
  });
});

describe('stories for every component (FR-UI-8)', () => {
  it('every component has sibling stories Light and Dark that render', async () => {
    const { componentFiles } = await load(() => import('./sourceScan'));
    expect(componentFiles).toBeTypeOf('function');
    const files = componentFiles();
    expect(files.length).toBeGreaterThan(5);
    const stories = import.meta.glob('../**/*.stories.tsx');
    const { composeStories } = await import('@storybook/react-vite');
    for (const file of files) {
      // Vite keys files of this test's own directory as './…' and others as '../…'.
      const rel = file.replace(/^src\//, '').replace(/\.tsx$/, '.stories.tsx');
      const importer = stories[`../${rel}`] ?? stories[`./${rel.replace(/^app\//, '')}`];
      expect(importer, `stories for ${file}`).toBeTypeOf('function');
      const composed = composeStories((await importer!()) as Parameters<typeof composeStories>[0]) as Record<
        string,
        React.ComponentType
      >;
      expect(Object.keys(composed)).toEqual(expect.arrayContaining(['Light', 'Dark']));
      for (const Story of Object.values(composed)) {
        const { unmount } = render(<Story />);
        unmount();
      }
    }
  }, 60_000);
});
