// @trace FR-RENDER-2 INV-RENDER-1 VEC-RENDER-1
import { cleanup, render } from '@testing-library/react';
import fc from 'fast-check';
import { afterEach, describe, expect, it } from 'vitest';
import { readJson, readYaml } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

type Case = { id: string; output: { data?: Record<string, unknown> } };
const cases = () => readYaml<{ cases: Case[] }>('spec/fixtures/WEB-RENDER/outputs.yaml').cases;
const explainSource = () =>
  String(
    readJson<{ cells: Array<{ id: string; source: string }> }>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb').cells.find(
      (c) => c.id === 'w01-explain',
    )?.source,
  );

const FORBIDDEN_ELEMENTS = ['script', 'style', 'img', 'iframe', 'object', 'embed', 'form', 'base'];
const SAFE_HREF = /^(https?:|mailto:|[^:]*$)/i;

/** Checks the rendered DOM against INV-RENDER-1. KaTeX and Shiki elements may carry style and class. */
const violations = (root: HTMLElement): string[] => {
  const out: string[] = [];
  for (const el of Array.from(root.querySelectorAll('*'))) {
    const tag = el.tagName.toLowerCase();
    if (FORBIDDEN_ELEMENTS.includes(tag)) out.push(`element ${tag}`);
    for (const attr of Array.from(el.attributes)) {
      if (/^on/i.test(attr.name)) out.push(`attribute ${attr.name}`);
      if (attr.name === 'srcset') out.push('srcset');
      if (attr.name === 'href' && !SAFE_HREF.test(attr.value.trim())) out.push(`href ${attr.value}`);
    }
    // KaTeX and Shiki output, and the module's own chrome (INV-RENDER-1, design pass: the element itself is marked).
    const trusted = el.closest('.katex, .shiki') ?? (el.hasAttribute('data-render-chrome') ? el : null);
    if (!trusted && el.hasAttribute('style')) out.push(`style on ${tag}`);
    if (!trusted && el.getAttribute('class')) {
      const allowed = tag === 'code' && el.getAttribute('class')!.split(/\s+/).every((c) => /^(language-[\w-]+|math-inline|math-display)$/.test(c));
      if (!allowed) out.push(`class on ${tag}: ${el.getAttribute('class')}`);
    }
  }
  return out;
};

describe('markdown pipeline (FR-RENDER-2)', () => {
  it('renders GFM tables, math and links with rel and target', async () => {
    const { Markdown } = await load(() => import('./markdown'));
    expect(Markdown).toBeTypeOf('function');
    const { container } = render(<Markdown source={'| a | b |\n|---|---|\n| 1 | 2 |\n\n$x^2$ [ext](https://example.com) [rel](/lessons/w01)'} />);
    expect(container.querySelector('table')).not.toBeNull();
    expect(container.querySelector('.katex')).not.toBeNull();
    const ext = container.querySelector('a[href="https://example.com"]');
    expect(ext?.getAttribute('rel')).toBe('noopener noreferrer');
    expect(ext?.getAttribute('target')).toBe('_blank');
    expect(container.querySelector('a[href="/lessons/w01"]')?.getAttribute('target')).toBeNull();
  });

  it('treats a protocol-relative link as absolute and keeps footnote ids and links in step', async () => {
    const { Markdown } = await load(() => import('./markdown'));
    expect(Markdown).toBeTypeOf('function');
    const { container } = render(<Markdown source={'[p](//example.com/x) note[^1]\n\n[^1]: the note'} />);
    expect(container.querySelector('a[href="//example.com/x"]')?.getAttribute('target')).toBe('_blank');
    const ref = container.querySelector('a[data-footnote-ref]') ?? container.querySelector('sup a');
    const target = ref?.getAttribute('href')?.slice(1) ?? '';
    expect(target).not.toBe('');
    expect(container.querySelector(`[id="${target}"]`)).not.toBeNull();
    expect(target).not.toMatch(/user-content-user-content-/);
  });

  it('keeps raw HTML as text and drops markdown images', async () => {
    const { Markdown } = await load(() => import('./markdown'));
    expect(Markdown).toBeTypeOf('function');
    const { container } = render(<Markdown source={'<b>bold</b> ![p](https://example.com/p.png)'} />);
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('<b>bold</b>');
    expect(container.querySelector('img')).toBeNull();
  });

  it('the W01 explain cell and the hostile VEC-RENDER-1 markdown render without violations', async () => {
    const { Markdown } = await load(() => import('./markdown'));
    expect(Markdown).toBeTypeOf('function');
    const hostile = cases()
      .map((c) => c.output.data?.['text/markdown'])
      .filter((s): s is string => typeof s === 'string');
    // Design pass (INV-RENDER-1, code review): a 5-column table, whose wrapper is the module's trusted chrome, and a
    // forged chrome mark in raw HTML, which stays text.
    const table = '| a | b | c | d | e |\n|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 |';
    const forgery = '<p data-render-chrome class="x">forged</p>';
    for (const source of [explainSource(), ...hostile, table, forgery]) {
      const { container, unmount } = render(<Markdown source={source} />);
      expect(violations(container)).toEqual([]);
      unmount();
    }
    const { container } = render(<Markdown source={`${table}\n\n${forgery}`} />);
    expect(container.querySelector('.table-wide[data-render-chrome] > table')).toBeTruthy();
    expect(container.querySelector('.x, p[data-render-chrome]')).toBeNull();
    expect(container.textContent).toContain('<p data-render-chrome class="x">forged</p>');
  });
});

describe('sanitiser invariant (INV-RENDER-1)', () => {
  it('holds for generated markup', async () => {
    const { Markdown } = await load(() => import('./markdown'));
    expect(Markdown).toBeTypeOf('function');
    const pieces = fc.constantFrom(
      '<script>x</script>', '<style>*{}</style>', '<img src=x onerror=y>', '<iframe src=//e>', '<a href="javascript:x">j</a>',
      '[j](javascript:alert(1))', '[d](data:text/html,x)', '[v](vbscript:x)', '<span style="position:fixed">s</span>',
      '<p class="x" id="y">c</p>', '![i](x.png)', '<img srcset="a 1x">', '<form><input></form>', '<base href=//e>',
      '`code`', '```python\nprint(1)\n```', '$a$', '$$b$$', '**b**', '\n\n', 'text', '<object data=x>', '<embed src=x>',
      '[ok](https://example.com)', '[m](mailto:a@b.c)', '<svg onload=x>', '<math><mtext>x</mtext></math>',
    );
    await fc.assert(
      fc.asyncProperty(fc.array(pieces, { maxLength: 8 }), async (parts) => {
        const { container, unmount } = render(<Markdown source={parts.join(' ')} />);
        const v = violations(container);
        unmount();
        return v.length === 0;
      }),
      { numRuns: 150 },
    );
  });
});
