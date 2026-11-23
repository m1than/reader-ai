// @trace FR-NOTEBOOK-11
// Lesson contents entries (code review 2026-11-23: table-driven headings and labels).
import { describe, expect, it } from 'vitest';
import { load } from '../app/testing/load';

describe('firstHeading (FR-NOTEBOOK-11)', () => {
  const cases: Array<[string, string, string | null]> = [
    ['plain ATX heading', '## Title', 'Title'],
    ['setext heading', 'Title\n=====', 'Title'],
    ['depth 4 is not an entry', '#### Deep', null],
    ['first of several', '### One\n\n## Two', 'One'],
    ['raw HTML gives no text', '## <b>Bold</b> word', 'Bold word'],
    ['an empty first heading is skipped', '##\n\n## Second', 'Second'],
    ['a heading of only HTML is skipped', '## <br>\n\n## Next', 'Next'],
    ['whitespace collapsed and trimmed', '##   Many    spaces   here  ', 'Many spaces here'],
    ['emphasis, code and links stripped', '## A **b** `c` [d](https://x.org)', 'A b c d'],
    ['inline math keeps its text', '## Bound $2^{n/2}$', 'Bound 2^{n/2}'],
    ['GFM strikethrough', '## ~~old~~ new', 'old new'],
    ['# inside a fence is not a heading', '```\n# comment\n```', null],
    ['# inside display math is not a heading', '$$\n# x\n$$', null],
    ['a heading inside a list is not top level', '- ## Item', null],
    ['a heading inside a quote is not top level', '> ## Quoted', null],
    ['no heading', 'Just text.', null],
  ];
  for (const [name, source, expected] of cases) {
    it(name, async () => {
      const { firstHeading } = await load(() => import('./contents'));
      expect(firstHeading).toBeTypeOf('function');
      expect(firstHeading(source)).toBe(expected);
    });
  }
});

type Cell = { cell_type: string; id: string; source: string; kind: string | undefined; status: unknown };
const cell = (id: string, kind: string | undefined, source = '', cell_type = 'markdown', status?: unknown): Cell => ({ cell_type, id, source, kind, status });

describe('cellLabels and contentsEntries (FR-NOTEBOOK-11)', () => {
  it('labels headed markdown cells and numbered labs, never code comments, recall or open cells', async () => {
    const { cellLabels } = await load(() => import('./contents'));
    expect(cellLabels).toBeTypeOf('function');
    const labels = cellLabels([
      cell('e', 'explain', '## Explained'),
      cell('c', undefined, '# a comment', 'code'),
      cell('l1', 'lab', '# Lab heading comment', 'code'),
      cell('r', 'recall', '## Recall heading'),
      cell('o', 'open', '## Open heading', 'markdown', 'open'),
      cell('u', undefined, '## Untyped markdown'),
      cell('l2', 'lab', 'print(2)', 'code'),
    ]);
    expect([...labels.entries()]).toEqual([
      ['e', 'Explained'],
      ['l1', 'Lab 1'],
      ['u', 'Untyped markdown'],
      ['l2', 'Lab 2'],
    ]);
  });

  it('adds one recall entry however many recall cells there are, and nothing without entries', async () => {
    const { cellLabels, contentsEntries } = await load(() => import('./contents'));
    expect(contentsEntries).toBeTypeOf('function');
    const cells = [cell('r1', 'recall', ''), cell('x', 'notes'), cell('r2', 'recall', '## Heading'), cell('r3', 'recall')];
    const entries = contentsEntries(cells, cellLabels(cells), false);
    expect(entries).toEqual([{ href: '#cell-r1', label: 'Recall cards (3)' }]);
    expect(contentsEntries([cell('n', 'notes')], new Map(), false)).toEqual([]);
  });
});
