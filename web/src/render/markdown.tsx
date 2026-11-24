// Markdown (cell sources and text/markdown outputs), WEB-RENDER FR-RENDER-2:
// remark (GFM, math) → raw HTML turned into text → rehype → sanitise → KaTeX → links → React; code blocks in
// `language-python` are highlighted by the lazy Code component (FR-RENDER-4).
import type { Element, ElementContent, Root } from 'hast';
import type { Root as MdRoot, RootContent as MdContent } from 'mdast';
import rehypeKatex from 'rehype-katex';
import rehypeSanitize, { defaultSchema, type Options as SanitizeSchema } from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import remarkParse from 'remark-parse';
import remarkRehype from 'remark-rehype';
import { unified } from 'unified';
import { Code } from './code';
import { hastToReact } from './hast';
import { dropAnnotations, KATEX_OPTIONS } from './katex';

const REMOVED_TAGS = new Set([
  'img', 'picture', 'source', 'video', 'audio', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'base', 'style',
]);

type AttrDef = NonNullable<SanitizeSchema['attributes']>[string][number];
const notClassName = (a: AttrDef) => !(a === 'className' || (Array.isArray(a) && a[0] === 'className'));

/** The default (GitHub) schema, narrowed per FR-RENDER-2. */
export const SANITIZE_SCHEMA: SanitizeSchema = {
  ...defaultSchema,
  tagNames: (defaultSchema.tagNames ?? []).filter((t) => !REMOVED_TAGS.has(t)),
  attributes: {
    ...Object.fromEntries(
      Object.entries(defaultSchema.attributes ?? {}).map(([tag, defs]) => [
        tag,
        defs.filter((d) => notClassName(d) && d !== 'style' && d !== 'srcSet'),
      ]),
    ),
    code: [['className', /^language-[\w-]+$/, 'math-inline', 'math-display']],
  },
  // Keep the default protocol rules for every other URL attribute; narrow only href.
  protocols: { ...defaultSchema.protocols, href: ['http', 'https', 'mailto'] },
};

/** remark plugin: raw HTML in markdown stays visible as text (no rehype-raw). */
const rawHtmlAsText = () => (tree: MdRoot) => {
  const visit = (node: { children?: MdContent[] }) => {
    if (!Array.isArray(node.children)) return;
    node.children = node.children.map((child) =>
      child.type === 'html' ? ({ type: 'text', value: child.value } as MdContent) : child,
    );
    for (const child of node.children) visit(child as { children?: MdContent[] });
  };
  visit(tree as { children?: MdContent[] });
};

/** rehype plugin: link attributes; absolute links open in a new tab. */
const linkAttributes = () => (tree: Root) => {
  const visit = (node: Root | Element) => {
    for (const child of node.children) {
      if (child.type !== 'element') continue;
      if (child.tagName === 'a') {
        child.properties.rel = ['noopener', 'noreferrer'];
        // A scheme, or a protocol-relative `//host`, leaves the app.
        if (/^([a-z][a-z0-9+.-]*:|\/\/)/i.test(String(child.properties.href ?? ''))) child.properties.target = '_blank';
      }
      visit(child);
    }
  };
  visit(tree);
};

/**
 * rehype plugin after sanitising: the sanitiser prefixes every `id` with `user-content-` (DOM clobbering guard), so
 * an in-page link `#x` (footnotes) is pointed at `#user-content-x` when that id exists — what GitHub does in script.
 */
const fragmentLinks = () => (tree: Root) => {
  const ids = new Set<string>();
  const collect = (node: Root | Element) => {
    for (const child of node.children) {
      if (child.type !== 'element') continue;
      if (typeof child.properties.id === 'string') ids.add(child.properties.id);
      collect(child);
    }
  };
  const rewrite = (node: Root | Element) => {
    for (const child of node.children) {
      if (child.type !== 'element') continue;
      const href = child.properties.href;
      if (child.tagName === 'a' && typeof href === 'string' && href.startsWith('#') && ids.has(`user-content-${href.slice(1)}`))
        child.properties.href = `#user-content-${href.slice(1)}`;
      rewrite(child);
    }
  };
  collect(tree);
  rewrite(tree);
};

type MdNode = { type: string; value?: string; url?: string; alt?: string | null; children?: MdNode[] };
const plain = (node: MdNode): string =>
  node.type === 'text' || node.type === 'inlineCode' || node.type === 'inlineMath'
    ? (node.value ?? '')
    : (node.children ?? []).map(plain).join('');

/**
 * remark plugin (option `linksAsText`, SRV-TUTOR VEC-TUTOR-6): a link becomes "label (url)" and an image its alt
 * text, as plain text, so nothing is linked or fetched. References and definitions are dropped the same way.
 */
const linksToText = () => (tree: MdRoot) => {
  const visit = (node: MdNode) => {
    if (!Array.isArray(node.children)) return;
    node.children = node.children.flatMap((child): MdNode[] => {
      if (child.type === 'link') {
        const label = plain(child);
        const url = child.url ?? '';
        return [{ type: 'text', value: label === url || label === '' ? url : `${label} (${url})` }];
      }
      if (child.type === 'linkReference') return [{ type: 'text', value: plain(child) }];
      if (child.type === 'image' || child.type === 'imageReference') return [{ type: 'text', value: child.alt ?? '' }];
      if (child.type === 'definition') return [];
      return [child];
    });
    for (const child of node.children) visit(child);
  };
  visit(tree as unknown as MdNode);
};

const makeProcessor = (linksAsText: boolean) =>
  unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkMath)
    .use(rawHtmlAsText)
    .use(linksAsText ? linksToText : () => undefined)
    // No prefix here: rehype-sanitize adds its clobber prefix once (footnote ids and their links stay in step).
    .use(remarkRehype, { clobberPrefix: '' })
    .use(rehypeSanitize, SANITIZE_SCHEMA)
    .use(fragmentLinks)
    .use(rehypeKatex, KATEX_OPTIONS)
    .use(dropAnnotations)
    .use(linkAttributes);

const processor = makeProcessor(false);
const textProcessor = makeProcessor(true);

export const markdownToHast = (source: string, linksAsText = false): Root => {
  const p = linksAsText ? textProcessor : processor;
  return p.runSync(p.parse(source)) as Root;
};

const textOf = (nodes: ElementContent[]): string =>
  nodes.map((n) => (n.type === 'text' ? n.value : n.type === 'element' ? textOf(n.children) : '')).join('');

/** A fenced block (`pre > code`) becomes the Code component with its header: the fence's language, else `text`;
 * python is highlighted lazily (FR-RENDER-4). */
const Pre = (props: { node?: Element; children?: React.ReactNode }) => {
  const code = props.node?.children.find((c): c is Element => c.type === 'element' && c.tagName === 'code');
  const classes = (code?.properties.className as string[] | undefined) ?? [];
  const language = classes.find((c) => c.startsWith('language-'))?.slice('language-'.length) ?? 'text';
  if (code) return <Code code={textOf(code.children).replace(/\n$/, '')} language={language} />;
  return <pre>{props.children}</pre>;
};

/** WEB-UI FR-UI-10: a table of 5 or more columns is a wide block. */
export const WIDE_TABLE_COLUMNS = 5;
const columnsOf = (table: Element | undefined): number => {
  const rows: Element[] = [];
  const walk = (n: Element) => {
    for (const c of n.children) if (c.type === 'element') (c.tagName === 'tr' ? rows.push(c) : walk(c));
  };
  if (table) walk(table);
  const first = rows[0];
  return first ? first.children.filter((c) => c.type === 'element' && (c.tagName === 'td' || c.tagName === 'th')).length : 0;
};
const Table = (props: { node?: Element; children?: React.ReactNode }) => {
  const table = <table>{props.children}</table>;
  return columnsOf(props.node) >= WIDE_TABLE_COLUMNS ? (
    <div className="wide table-wide" data-render-chrome="">
      {table}
    </div>
  ) : (
    table
  );
};

/** `lang` (WEB-UI FR-UI-32): the content's own language, set on this content node; omitted, it inherits. */
export function Markdown({ source, linksAsText = false, lang }: { source: string; linksAsText?: boolean; lang?: string | undefined }) {
  return <div data-markdown="" lang={lang}>{hastToReact(markdownToHast(source, linksAsText), { pre: Pre as never, table: Table as never })}</div>;
}
