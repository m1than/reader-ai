// @trace INV-UI-2 FR-UI-8
import { cleanup, render } from '@testing-library/react';
import type * as React from 'react';
import { afterEach, describe, expect, it } from 'vitest';
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
      const storyPath = `../${file.replace(/^src\//, '').replace(/\.tsx$/, '.stories.tsx')}`;
      const importer = stories[storyPath];
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
