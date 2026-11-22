// text/latex outputs (WEB-RENDER FR-RENDER-3): one outer delimiter pair removed, display mode, KaTeX through the
// same hast path as markdown (no HTML string), overflow clipped by the container.
import type { Root } from 'hast';
import rehypeKatex from 'rehype-katex';
import { unified } from 'unified';
import { hastToReact } from './hast';
import { dropAnnotations, KATEX_OPTIONS } from './katex';

export { KATEX_OPTIONS };

const PAIRS: Array<[string, string]> = [
  ['$$', '$$'],
  ['\\[', '\\]'],
  ['\\(', '\\)'],
  ['$', '$'],
];

export const stripDelimiters = (source: string): string => {
  const s = source.trim();
  for (const [open, close] of PAIRS) {
    if (s.length >= open.length + close.length && s.startsWith(open) && s.endsWith(close))
      return s.slice(open.length, s.length - close.length);
  }
  return source;
};

const katex = unified().use(rehypeKatex, KATEX_OPTIONS).use(dropAnnotations);

const toHast = (tex: string): Root => {
  const tree: Root = {
    type: 'root',
    children: [
      {
        type: 'element',
        tagName: 'pre',
        properties: {},
        children: [
          { type: 'element', tagName: 'code', properties: { className: ['language-math', 'math-display'] }, children: [{ type: 'text', value: tex }] },
        ],
      },
    ],
  };
  return katex.runSync(tree) as Root;
};

export function Latex({ source }: { source: string }) {
  return (
    <div className="math-box" data-math="display">
      {hastToReact(toHast(stripDelimiters(source)))}
    </div>
  );
}
