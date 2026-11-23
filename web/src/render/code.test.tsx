// @trace FR-RENDER-4
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { load } from '../app/testing/load';

afterEach(cleanup);

describe('Shiki (FR-RENDER-4)', () => {
  it('highlights python into a hast tree with the JavaScript engine', async () => {
    const { highlight } = await load(() => import('./highlight'));
    expect(highlight).toBeTypeOf('function');
    // Design pass: both schemes at once, so no scheme argument.
    const tree = await highlight('print(1)', 'python');
    expect(tree?.type).toBe('root');
    expect(JSON.stringify(tree)).toContain('print');
  });

  it('returns null for other languages', async () => {
    const { highlight } = await load(() => import('./highlight'));
    expect(highlight).toBeTypeOf('function');
    expect(await highlight('fn main() {}', 'rust')).toBeNull();
  });

  it('renders plain monospace first, then highlighted tokens as React elements', async () => {
    const { Code } = await load(() => import('./code'));
    expect(Code).toBeTypeOf('function');
    const { container } = render(<Code code={'x = 1\nprint(x)'} language="python" />);
    expect(container.querySelector('pre')?.textContent).toBe('x = 1\nprint(x)');
    await waitFor(() => expect(container.querySelector('pre.shiki')).not.toBeNull());
    expect(container.querySelector('pre')?.textContent).toBe('x = 1\nprint(x)');
  });

  it('keeps other languages as plain text', async () => {
    const { Code } = await load(() => import('./code'));
    expect(Code).toBeTypeOf('function');
    const { container } = render(<Code code={'<b>x</b>'} language="html" />);
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toBe('<b>x</b>');
  });
});
