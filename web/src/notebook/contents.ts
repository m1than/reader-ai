// Lesson contents (WEB-NOTEBOOK FR-NOTEBOOK-11): entries derived from the cells. Headings come from the markdown
// parse (remark with GFM and math, as WEB-RENDER), so a `#` line inside a code fence is not a heading.
import type { Nodes, Root } from 'mdast';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import remarkParse from 'remark-parse';
import { unified } from 'unified';

export interface ContentsCell {
  cell_type: string;
  id: string;
  source: string;
  kind: string | undefined;
  status: unknown;
}
export interface ContentsEntry {
  href: string;
  label: string;
}

const parser = unified().use(remarkParse).use(remarkGfm).use(remarkMath);

const plain = (node: Nodes): string => {
  if ('value' in node && typeof node.value === 'string' && node.type !== 'html') return node.value;
  if ('children' in node) return (node.children as Nodes[]).map(plain).join('');
  return '';
};

/** The plain text of the first heading of depth 1–3 at the top level of a markdown source, if any. */
export const firstHeading = (markdown: string): string | null => {
  const tree = parser.parse(markdown) as Root;
  for (const node of tree.children) {
    if (node.type === 'heading' && node.depth <= 3) {
      const text = plain(node).replace(/\s+/g, ' ').trim();
      if (text) return text;
    }
  }
  return null;
};

export const isUnansweredOpen = (c: Pick<ContentsCell, 'kind' | 'status'>) => c.kind === 'open' && c.status !== 'answered';

/** The label of every cell that has an entry of its own (headed markdown cells and labs), by cell id. */
export const cellLabels = (cells: ContentsCell[]): Map<string, string> => {
  const labels = new Map<string, string>();
  let lab = 0;
  for (const c of cells) {
    if (c.kind === 'lab') {
      lab += 1;
      labels.set(c.id, `Lab ${lab}`);
    } else if (c.cell_type === 'markdown' && c.kind !== 'recall' && c.kind !== 'open') {
      const h = firstHeading(c.source);
      if (h) labels.set(c.id, h);
    }
  }
  return labels;
};

export const contentsEntries = (cells: ContentsCell[], labels: Map<string, string>, tutor: boolean): ContentsEntry[] => {
  const entries: ContentsEntry[] = [];
  const recall = cells.filter((c) => c.kind === 'recall');
  const open = cells.filter(isUnansweredOpen);
  for (const c of cells) {
    const label = labels.get(c.id);
    if (label) entries.push({ href: `#cell-${c.id}`, label });
    if (c === recall[0]) entries.push({ href: `#cell-${c.id}`, label: `Recall cards (${recall.length})` });
    if (c === open[0]) entries.push({ href: `#cell-${c.id}`, label: `Open questions (${open.length})` });
  }
  if (tutor) entries.push({ href: '#tutor', label: 'Ask the tutor' });
  return entries;
};
