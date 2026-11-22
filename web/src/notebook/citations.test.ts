// @trace FR-NOTEBOOK-2 VEC-NOTEBOOK-1
import { describe, expect, it } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

type Cell = { id: string; metadata: { reader?: Record<string, unknown> } };
const cell = (id: string) =>
  readJson<{ cells: Cell[] }>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb').cells.find((c) => c.id === id)!;

describe('citations (FR-NOTEBOOK-2)', () => {
  it('formats span citations with title and page, title only, or the span id', async () => {
    const { formatCitation } = await load(() => import('./citations'));
    expect(formatCitation).toBeTypeOf('function');
    const span = 'a'.repeat(64) + ':12';
    expect(formatCitation({ span, page: 12, material_title: 'Book' })).toEqual({ kind: 'text', text: 'Book, p. 12' });
    expect(formatCitation({ span, material_title: 'Book' })).toEqual({ kind: 'text', text: 'Book' });
    expect(formatCitation({ span })).toEqual({ kind: 'text', text: span });
  });

  it('links only http and https URLs, with host and path as text and the retrieved date', async () => {
    const { formatCitation } = await load(() => import('./citations'));
    expect(formatCitation).toBeTypeOf('function');
    expect(formatCitation({ url: 'https://en.wikipedia.org/wiki/Birthday_attack', retrieved: '2026-11-21' })).toEqual({
      kind: 'link',
      href: 'https://en.wikipedia.org/wiki/Birthday_attack',
      text: 'en.wikipedia.org/wiki/Birthday_attack',
      retrieved: '2026-11-21',
    });
    expect(formatCitation({ url: 'javascript:alert(4)', retrieved: '2026-11-21' })).toEqual({
      kind: 'text',
      text: 'javascript:alert(4)',
      retrieved: '2026-11-21',
    });
  });

  it('collects the W01 explain cell citations numbered, deduplicated, in first-use order', async () => {
    const { collectCitations } = await load(() => import('./citations'));
    expect(collectCitations).toBeTypeOf('function');
    const refs = collectCitations(cell('w01-explain').metadata.reader ?? {});
    expect(refs.map((r) => r.text)).toEqual([
      'Fixture material, p. 12',
      'Fixture material, p. 14',
      'b'.repeat(64) + ':3',
      'en.wikipedia.org/wiki/Birthday_attack',
      'javascript:alert(4)',
    ]);
  });

  it('lists route source_refs and the erratum source_ref the same way', async () => {
    const { collectCitations } = await load(() => import('./citations'));
    expect(collectCitations).toBeTypeOf('function');
    expect(collectCitations(cell('w01-route').metadata.reader ?? {}).map((r) => r.text)).toEqual([
      'Fixture material, p. 12',
      'Fixture material, p. 14',
    ]);
    expect(collectCitations(cell('w01-erratum').metadata.reader ?? {}).map((r) => r.text)).toEqual(['Fixture material, p. 14']);
  });
});
