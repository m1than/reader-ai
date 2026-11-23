// @trace FR-RENDER-4 FR-UI-10
// Design pass (2026-11-23, V/design-pass): dual-theme Shiki through CSS custom properties, the code block header with
// the language and Copy, and code and wide tables as wide blocks.
import { act, cleanup, fireEvent, render, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { load } from '../app/testing/load';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const stubClipboard = (fail = false) => {
  const writeText = vi.fn(async () => {
    if (fail) throw new Error('denied');
  });
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  return writeText;
};
const styleOf = (el: Element | null) => el?.getAttribute('style') ?? '';

describe('dual-theme Shiki (FR-RENDER-4, design pass)', () => {
  it('gives every token both colours and the block both backgrounds', async () => {
    const { highlight } = await load(() => import('./highlight'));
    expect(highlight).toBeTypeOf('function');
    const tree = await highlight('def f(x):\n    return 1  # c', 'python');
    const text = JSON.stringify(tree);
    expect(text).toContain('--shiki-light');
    expect(text).toContain('--shiki-dark');
    expect(text).toContain('--shiki-light-bg');
    expect(text).toContain('--shiki-dark-bg');
  });

  it('renders the custom properties on the page and never asks matchMedia while rendering', async () => {
    const spy = vi.fn(() => ({ matches: true, addEventListener: () => undefined, removeEventListener: () => undefined }));
    Object.defineProperty(window, 'matchMedia', { value: spy, configurable: true, writable: true });
    const { Code } = await load(() => import('./code'));
    const { container } = render(<Code code={'x = 1'} language="python" />);
    await waitFor(() => expect(container.querySelector('pre.shiki')).not.toBeNull());
    const token = container.querySelector('pre.shiki span[style]');
    expect(styleOf(token)).toMatch(/--shiki-light:/);
    expect(styleOf(token)).toMatch(/--shiki-dark:/);
    expect(styleOf(container.querySelector('pre.shiki'))).toMatch(/--shiki-dark-bg:/);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('code block header (FR-RENDER-4, design pass)', () => {
  it('names the language: python, a named fence, or text', async () => {
    const { Markdown } = await load(() => import('./markdown'));
    expect(Markdown).toBeTypeOf('function');
    const { container } = render(<Markdown source={'```python\nx = 1\n```\n\n```rust\nfn main() {}\n```\n\n```\nplain\n```'} />);
    const headers = [...container.querySelectorAll('.code-block .code-header')].map((h) => h.querySelector('.code-lang')?.textContent);
    expect(headers).toEqual(['python', 'rust', 'text']);
    for (const block of container.querySelectorAll('.code-block')) expect(block.classList.contains('wide')).toBe(true);
  });

  it('copies exactly the code, then reads Copied and announces it', async () => {
    const writeText = stubClipboard();
    const { Code } = await load(() => import('./code'));
    const code = 'for i in range(3):\n    print(i)';
    const { container } = render(<Code code={code} language="python" />);
    const block = container.querySelector('.code-block') as HTMLElement;
    await act(async () => {
      fireEvent.click(within(block).getByRole('button', { name: 'Copy code' }));
    });
    expect(writeText).toHaveBeenCalledWith(code);
    expect(within(block).getByText('Copied')).toBeTruthy();
    expect(within(block).getByRole('status').textContent).toBe('Code copied');
  });

  it('says when copying failed', async () => {
    stubClipboard(true);
    const { Code } = await load(() => import('./code'));
    const { container } = render(<Code code={'x'} language="python" />);
    const block = container.querySelector('.code-block') as HTMLElement;
    await act(async () => {
      fireEvent.click(within(block).getByRole('button', { name: 'Copy code' }));
    });
    expect(within(block).getByRole('status').textContent).toBe('Copy failed');
  });
});

describe('wide tables (FR-UI-10, design pass)', () => {
  it('marks markdown tables of 5 or more columns wide, narrower ones not', async () => {
    const { Markdown } = await load(() => import('./markdown'));
    const five = '| a | b | c | d | e |\n|---|---|---|---|---|\n| 1 | 2 | 3 | 4 | 5 |';
    const four = '| a | b | c | d |\n|---|---|---|---|\n| 1 | 2 | 3 | 4 |';
    const { container } = render(<Markdown source={`${five}\n\n${four}`} />);
    const tables = [...container.querySelectorAll('table')];
    expect(tables).toHaveLength(2);
    expect(tables[0]!.closest('.wide')).toBeTruthy();
    expect(tables[1]!.closest('.wide')).toBeNull();
  });
});
