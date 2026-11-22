// @trace FR-RENDER-1 FR-RENDER-5 INV-RENDER-2 VEC-RENDER-1
import { cleanup, render } from '@testing-library/react';
import fc from 'fast-check';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readYaml } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

type Case = { id: string; output: Record<string, unknown>; treatment: string; expect_text?: string; expect_alt?: string };
const fixture = () => readYaml<{ preference: string[]; cases: Case[] }>('spec/fixtures/WEB-RENDER/outputs.yaml');

const created: Blob[] = [];
beforeEach(() => {
  created.length = 0;
  URL.createObjectURL = vi.fn((b: Blob) => {
    created.push(b);
    return `blob:test/${created.length}`;
  });
  URL.revokeObjectURL = vi.fn();
});
afterEach(cleanup);

describe('dispatch (FR-RENDER-1)', () => {
  it('uses the preference order of VEC-RENDER-1', async () => {
    const { PREFERENCE, pickMime } = await load(() => import('./outputs'));
    expect(PREFERENCE).toEqual(fixture().preference);
    expect(pickMime({ 'text/plain': 'x', 'image/png': 'y' })).toBe('image/png');
    expect(pickMime({ 'application/x-unknown': '?' })).toBeNull();
  });

  it('renders every VEC-RENDER-1 case with its treatment', async () => {
    const { Output } = await load(() => import('./outputs'));
    expect(Output).toBeTypeOf('function');
    for (const c of fixture().cases) {
      const { container, unmount } = render(<Output output={c.output as never} />);
      const kind = container.firstElementChild?.getAttribute('data-output');
      expect(kind, c.id).toBe(c.treatment);
      if (c.expect_text) expect(container.textContent, c.id).toContain(c.expect_text);
      if (c.treatment === 'image') {
        const img = container.querySelector('img');
        expect(img?.getAttribute('src'), c.id).toMatch(/^blob:/);
        if (c.expect_alt) expect(img?.getAttribute('alt')).toBe(c.expect_alt);
      }
      if (c.treatment === 'dropped') expect(container.textContent).toMatch(/unsupported output type: application\/x-unknown/);
      expect(container.querySelector('script, iframe')).toBeNull();
      unmount();
    }
  });
});

describe('safe types (FR-RENDER-5)', () => {
  it('strips ANSI, pretty-prints JSON, revokes blob URLs on unmount', async () => {
    const { Output } = await load(() => import('./outputs'));
    expect(Output).toBeTypeOf('function');
    const err = render(<Output output={{ output_type: 'error', ename: 'E', evalue: 'v', traceback: ['\u001b[31mE\u001b[0m: v', 'line 2'] }} />);
    expect(err.container.textContent).toBe('E: v\nline 2');
    err.unmount();
    const json = render(<Output output={{ output_type: 'execute_result', execution_count: 1, data: { 'application/json': { a: [1] } }, metadata: {} }} />);
    expect(json.container.textContent).toBe('{\n  "a": [\n    1\n  ]\n}');
    json.unmount();
    const svg = render(<Output output={{ output_type: 'display_data', data: { 'image/svg+xml': '<svg/>' }, metadata: {} }} />);
    expect(created[0]?.type).toBe('image/svg+xml');
    svg.unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test/1');
  });

  it('cuts a text output at 1 MiB with a note', async () => {
    const { Output, TEXT_LIMIT } = await load(() => import('./outputs'));
    expect(TEXT_LIMIT).toBe(1024 * 1024);
    const { container } = render(<Output output={{ output_type: 'stream', name: 'stdout', text: 'x'.repeat(TEXT_LIMIT + 10) }} />);
    expect(container.querySelector('pre')?.textContent).toHaveLength(TEXT_LIMIT);
    expect(container.textContent).toMatch(/truncated/);
  });
});

describe('HTML never parsed (INV-RENDER-2)', () => {
  it('any text/html renders as text equal to text/plain or the source; SVG only via blob img', async () => {
    const { Output } = await load(() => import('./outputs'));
    expect(Output).toBeTypeOf('function');
    await fc.assert(
      fc.property(fc.string(), fc.option(fc.string(), { nil: undefined }), (html, plain) => {
        const data: Record<string, string> = { 'text/html': `<p onclick=x>${html}</p><script>y</script>` };
        if (plain !== undefined) data['text/plain'] = plain;
        const { container, unmount } = render(<Output output={{ output_type: 'display_data', data, metadata: {} }} />);
        const ok =
          container.querySelector('p, script') === null &&
          container.querySelector('pre')?.textContent === (plain ?? data['text/html']);
        unmount();
        return ok;
      }),
      { numRuns: 100 },
    );
    const { container } = render(<Output output={{ output_type: 'display_data', data: { 'image/svg+xml': '<svg><script>x</script></svg>' }, metadata: {} }} />);
    expect(container.querySelector('svg, script')).toBeNull();
    expect(container.querySelector('img')?.getAttribute('src')).toMatch(/^blob:/);
  });
});
