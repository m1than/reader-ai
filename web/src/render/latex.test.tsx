// @trace FR-RENDER-3 VEC-RENDER-1
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { load } from '../app/testing/load';

afterEach(cleanup);

describe('KaTeX (FR-RENDER-3)', () => {
  it('strips one outer delimiter pair', async () => {
    const { stripDelimiters } = await load(() => import('./latex'));
    expect(stripDelimiters).toBeTypeOf('function');
    expect(stripDelimiters('$$x$$')).toBe('x');
    expect(stripDelimiters('$x$')).toBe('x');
    expect(stripDelimiters('\\[x\\]')).toBe('x');
    expect(stripDelimiters('\\(x\\)')).toBe('x');
    expect(stripDelimiters(' $$ $x$ $$ ')).toBe(' $x$ ');
    expect(stripDelimiters('x')).toBe('x');
  });

  it('renders display math and never turns \\href or \\url into links', async () => {
    const { Latex } = await load(() => import('./latex'));
    expect(Latex).toBeTypeOf('function');
    const { container } = render(<Latex source={'$$\\href{javascript:alert(1)}{x} + \\url{https://e.com} + 2^{n/2}$$'} />);
    expect(container.querySelector('.katex-display')).not.toBeNull();
    expect(container.querySelector('a')).toBeNull();
    expect(container.innerHTML).not.toContain('javascript:');
  });

  it('renders a parse error as source in the error colour', async () => {
    const { Latex } = await load(() => import('./latex'));
    expect(Latex).toBeTypeOf('function');
    const { container } = render(<Latex source={'$\\frac{$'} />);
    expect(container.querySelector('.katex-error')).not.toBeNull();
  });

  it('caps sizes with maxSize and clips overflow', async () => {
    const { Latex, KATEX_OPTIONS } = await load(() => import('./latex'));
    expect(Latex).toBeTypeOf('function');
    expect(KATEX_OPTIONS).toMatchObject({ trust: false, strict: 'ignore', throwOnError: false, maxSize: 10, maxExpand: 1000, output: 'htmlAndMathml' });
    const { container } = render(<Latex source={'$\\rule{1000em}{1000em}$'} />);
    const box = container.firstElementChild as HTMLElement;
    expect(box.getAttribute('data-math')).toBe('display');
    expect(container.innerHTML).not.toMatch(/1000em/);
  });
});
