// KaTeX options shared by markdown math and text/latex outputs (WEB-RENDER FR-RENDER-3).
import type { Element, Root } from 'hast';
import type { KatexOptions } from 'katex';

/**
 * rehype plugin run after rehype-katex: removes MathML `annotation` elements, which repeat the TeX source as text
 * (so a `\href{javascript:…}` or an oversized `\rule` in the source never appears in the document, even as text).
 */
export const dropAnnotations = () => (tree: Root) => {
  const visit = (node: Root | Element) => {
    node.children = node.children.filter((c) => !(c.type === 'element' && c.tagName === 'annotation')) as typeof node.children;
    for (const c of node.children) if (c.type === 'element') visit(c);
  };
  visit(tree);
};

export const KATEX_OPTIONS = {
  trust: false,
  strict: 'ignore',
  throwOnError: false,
  maxSize: 10,
  maxExpand: 1000,
  output: 'htmlAndMathml',
} as const satisfies KatexOptions;
