// @trace FR-NOTEBOOK-2 VEC-NOTEBOOK-1
import { describe, expect, it } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

type Cell = { id: string; metadata: { reader?: Record<string, unknown> } };
const cell = (id: string) =>
  readJson<{ cells: Cell[] }>('spec/fixtures/WEB-NOTEBOOK/lesson-w01.ipynb').cells.find((c) => c.id === id)!;

describe('citations (FR-NOTEBOOK-2)', () => {
  it('formats span citations with title and page, title only, or "source" and the material id prefix', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const span = 'a'.repeat(64) + ':12';
    expect(groupCitations([{ span, page: 12, material_title: 'Book' }])).toEqual([{ kind: 'text', text: 'Book, p. 12' }]);
    expect(groupCitations([{ span, material_title: 'Book' }])).toEqual([{ kind: 'text', text: 'Book' }]);
    expect(groupCitations([{ span }])).toEqual([{ kind: 'text', text: 'source aaaaaaaa' }]);
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

  it('collects the W01 explain cell citations one entry per source, in first-use order', async () => {
    const { collectCitations } = await load(() => import('./citations'));
    expect(collectCitations).toBeTypeOf('function');
    const refs = collectCitations(cell('w01-explain').metadata.reader ?? {});
    expect(refs.map((r) => r.text)).toEqual([
      'Fixture material, pp. 12, 14',
      'source bbbbbbbb',
      'en.wikipedia.org/wiki/Birthday_attack',
      'javascript:alert(4)',
    ]);
  });

  it('lists route source_refs and the erratum source_ref the same way', async () => {
    const { collectCitations } = await load(() => import('./citations'));
    expect(collectCitations).toBeTypeOf('function');
    expect(collectCitations(cell('w01-route').metadata.reader ?? {}).map((r) => r.text)).toEqual(['Fixture material, pp. 12, 14']);
    expect(collectCitations(cell('w01-erratum').metadata.reader ?? {}).map((r) => r.text)).toEqual(['Fixture material, p. 14']);
  });
});

// Shapes of the live L02 (2026-11-23): many spans of one material, several per page; synthetic ids and titles.
const M = 'c'.repeat(64);
const N = 'd'.repeat(64);
const span = (m: string, seq: number, page?: number, title?: string) => ({
  span: `${m}:${seq}`,
  ...(page === undefined ? {} : { page }),
  ...(title === undefined ? {} : { material_title: title }),
});

describe('grouped references (FR-NOTEBOOK-2, amended 2026-11-23)', () => {
  it('collapses 16 spans of one page into one entry', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const many = Array.from({ length: 16 }, (_, i) => span(M, 400 + i, 70, 'Book'));
    expect(groupCitations(many).map((r) => r.text)).toEqual(['Book, p. 70']);
  });

  it('writes runs of consecutive pages as en-dash ranges, ascending, whatever the citation order', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const cites = [75, 70, 69, 70, 72, 73, 74, 71, 80, 82].map((p, i) => span(M, i, p, 'Book'));
    expect(groupCitations(cites).map((r) => r.text)).toEqual(['Book, pp. 69–75, 80, 82']);
    expect(groupCitations([span(M, 1, 69, 'Book'), span(M, 2, 70, 'Book')]).map((r) => r.text)).toEqual(['Book, pp. 69–70']);
  });

  it('keeps one entry per material in first-use order, URLs between them where first used', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const refs = groupCitations([
      span(N, 1, 5, 'Second'),
      { url: 'https://example.org/a', retrieved: '2026-11-21' },
      span(M, 2, 9, 'First'),
      span(N, 3, 6, 'Second'),
      { url: 'https://example.org/a', retrieved: '2026-11-21' },
    ]);
    expect(refs.map((r) => r.text)).toEqual(['Second, pp. 5–6', 'example.org/a', 'First, p. 9']);
  });

  it('takes a missing title from the lesson, else "source" and the first 8 characters of the material id', async () => {
    const { groupCitations, lessonTitles } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    expect(lessonTitles).toBeTypeOf('function');
    const titles = lessonTitles([
      { metadata: { reader: { kind: 'explain', claims: [{ citations: [span(M, 9, 61, 'Book')] }] } } },
      { metadata: { reader: { kind: 'lab' } } },
    ]);
    expect(groupCitations([span(M, 1, 60), span(M, 2, 60)], titles).map((r) => r.text)).toEqual(['Book, p. 60']);
    expect(groupCitations([span(N, 1, 60), span(N, 2)], titles).map((r) => r.text)).toEqual(['source dddddddd, p. 60']);
    expect(groupCitations([span(N, 2)]).map((r) => r.text)).toEqual(['source dddddddd']);
  });

  it('ignores pages that are not integers ≥ 1 and citations without span or url; a span without ":" is its own material', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const odd = [
      { span: `${M}:1`, page: Number.NaN, material_title: 'Book' },
      { span: `${M}:2`, page: '7', material_title: 'Book' },
      { span: `${M}:3`, page: 0, material_title: 'Book' },
      { span: `${M}:4`, page: -2, material_title: 'Book' },
      { span: `${M}:5`, page: 2.5, material_title: 'Book' },
      { span: `${M}:6`, page: Number.POSITIVE_INFINITY, material_title: 'Book' },
    ];
    expect(groupCitations(odd).map((r) => r.text)).toEqual(['Book']);
    expect(groupCitations([...odd, { span: `${M}:7`, page: 3, material_title: 'Book' }]).map((r) => r.text)).toEqual(['Book, p. 3']);
    expect(groupCitations([{ page: 4, material_title: 'Ghost' }, {}, { span: '', url: '' }, { span: 7 }]).map((r) => r.text)).toEqual([]);
    expect(
      groupCitations([
        { span: 'nocolon', page: 3, material_title: 'Plain' },
        { span: 'nocolon', page: 4 },
        { span: 'other' },
      ]).map((r) => r.text),
    ).toEqual(['Plain, pp. 3–4', 'source other']);
  });

  it('drops an empty material id and pages above the safe-integer range', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    expect(groupCitations([{ span: ':3', page: 3, material_title: 'Nobody' }]).map((r) => r.text)).toEqual([]);
    expect(groupCitations([{ span: `${M}:1`, page: 2 ** 53, material_title: 'Book' }]).map((r) => r.text)).toEqual(['Book']);
  });
});

describe('grouping details (FR-NOTEBOOK-2, code review 2026-11-23)', () => {
  it('sorts pages numerically, not as text', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const cites = [100, 9, 10, 99].map((p, i) => span(M, i, p, 'Book'));
    expect(groupCitations(cites).map((r) => r.text)).toEqual(['Book, pp. 9–10, 99–100']);
  });

  it('groups by the part before the last colon', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const refs = groupCitations([
      { span: 'a:b:3', page: 3, material_title: 'AB' },
      { span: 'a:c:1', page: 1, material_title: 'AC' },
      { span: 'a:b:4', page: 4, material_title: 'AB' },
    ]);
    expect(refs.map((r) => r.text)).toEqual(['AB, pp. 3–4', 'AC, p. 1']);
  });

  it('prefers the first title in the cell over the lesson title', async () => {
    const { groupCitations } = await load(() => import('./citations'));
    expect(groupCitations).toBeTypeOf('function');
    const titles = new Map([[M, 'Lesson title']]);
    expect(groupCitations([span(M, 1, 5, 'Cell title')], titles).map((r) => r.text)).toEqual(['Cell title, p. 5']);
    expect(groupCitations([span(M, 1, 5), span(M, 2, 6, 'First'), span(M, 3, 7, 'Second')], titles).map((r) => r.text)).toEqual([
      'First, pp. 5–7',
    ]);
  });

  it('takes the first title per material for the lesson', async () => {
    const { lessonTitles } = await load(() => import('./citations'));
    expect(lessonTitles).toBeTypeOf('function');
    const titles = lessonTitles([
      { metadata: { reader: { citations: [span(M, 1, 1, 'One'), span(N, 1, 1, 'Other')] } } },
      { metadata: { reader: { source_refs: [span(M, 2, 2, 'Two')] } } },
    ]);
    expect(titles.get(M)).toBe('One');
    expect(titles.get(N)).toBe('Other');
  });

  it('reads cell-level citations[] and treats a citation with url and span as a URL citation', async () => {
    const { collectCitations } = await load(() => import('./citations'));
    expect(collectCitations).toBeTypeOf('function');
    const refs = collectCitations({
      citations: [span(M, 1, 4, 'Book'), { url: 'https://example.org/x', span: `${M}:2`, page: 9, material_title: 'Book' }],
    });
    expect(refs).toEqual([
      { kind: 'text', text: 'Book, p. 4' },
      { kind: 'link', href: 'https://example.org/x', text: 'example.org/x', retrieved: '' },
    ]);
  });
});
