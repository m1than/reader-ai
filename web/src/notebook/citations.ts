// Citations as references (WEB-NOTEBOOK FR-NOTEBOOK-2). Span form `{span, page?, material_title?}` (SRV-LESSON) or
// `{url, retrieved}`. Span citations are grouped per material with page ranges; only http and https URLs become links.

export type Citation = { span?: unknown; page?: unknown; material_title?: unknown; url?: unknown; retrieved?: unknown };
export type Reference =
  | { kind: 'text'; text: string; retrieved?: string }
  | { kind: 'link'; href: string; text: string; retrieved: string };

/** Material id → the first `material_title` seen for it in the lesson. */
export type Titles = ReadonlyMap<string, string>;

const str = (v: unknown): string | undefined => (typeof v === 'string' && v.length > 0 ? v : undefined);

/** A URL citation: a link only for http and https, plain text otherwise. */
export const formatCitation = (c: Citation): Reference => {
  const url = str(c.url) ?? '';
  const retrieved = str(c.retrieved) ?? '';
  let parsed: URL | null = null;
  try {
    parsed = new URL(url);
  } catch {
    parsed = null;
  }
  if (parsed && (parsed.protocol === 'http:' || parsed.protocol === 'https:'))
    return { kind: 'link', href: parsed.href, text: `${parsed.host}${parsed.pathname === '/' ? '' : parsed.pathname}`, retrieved };
  return { kind: 'text', text: url, retrieved };
};

const materialOf = (span: string): string => {
  const i = span.lastIndexOf(':');
  return i < 0 ? span : span.slice(0, i);
};

const pageOf = (v: unknown): number | undefined => (typeof v === 'number' && Number.isSafeInteger(v) && v >= 1 ? v : undefined);

/** Distinct pages ascending, consecutive runs as en-dash ranges: [69, 70, 75] → "69–70, 75". */
export const pageRanges = (pages: number[]): string => {
  const sorted = [...new Set(pages)].sort((a, b) => a - b);
  const runs: string[] = [];
  for (let i = 0; i < sorted.length; ) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j]! + 1) j += 1;
    runs.push(i === j ? `${sorted[i]}` : `${sorted[i]}–${sorted[j]}`);
    i = j + 1;
  }
  return runs.join(', ');
};

const asList = (v: unknown): Citation[] => (Array.isArray(v) ? (v.filter((x) => x && typeof x === 'object') as Citation[]) : []);

/** Every citation object of a cell's `metadata.reader`, in order of use. */
export const cellCitations = (reader: Record<string, unknown>): Citation[] => {
  const claims = Array.isArray(reader.claims) ? (reader.claims as Array<{ citations?: unknown } | null>) : [];
  return [
    ...claims.flatMap((claim) => asList(claim?.citations)),
    ...asList(reader.citations),
    ...asList(reader.source_refs),
    ...(reader.source_ref && typeof reader.source_ref === 'object' ? [reader.source_ref as Citation] : []),
  ];
};

/** Titles of every material cited anywhere in the lesson (the first title seen per material). */
export const lessonTitles = (cells: Array<{ metadata?: Record<string, unknown> }>): Titles => {
  const titles = new Map<string, string>();
  for (const cell of cells) {
    const r = cell.metadata?.reader;
    if (!r || typeof r !== 'object') continue;
    for (const c of cellCitations(r as Record<string, unknown>)) {
      const span = str(c.span);
      const title = str(c.material_title);
      if (span && title && !titles.has(materialOf(span))) titles.set(materialOf(span), title);
    }
  }
  return titles;
};

type Entry = { kind: 'material'; id: string; title?: string; pages: number[] } | { kind: 'url'; citation: Citation };

/** One reference per material (pages as ranges) and per distinct URL, in order of first use. */
export const groupCitations = (citations: Citation[], titles: Titles = new Map()): Reference[] => {
  const entries: Entry[] = [];
  const byKey = new Map<string, Entry>();
  for (const c of citations) {
    const url = str(c.url);
    const span = str(c.span);
    if (url !== undefined) {
      const key = `url:${url}`;
      if (!byKey.has(key)) {
        const e: Entry = { kind: 'url', citation: c };
        byKey.set(key, e);
        entries.push(e);
      }
      continue;
    }
    if (span === undefined) continue;
    const id = materialOf(span);
    if (id === '') continue;
    const key = `span:${id}`;
    let e = byKey.get(key);
    if (!e) {
      e = { kind: 'material', id, pages: [] };
      byKey.set(key, e);
      entries.push(e);
    }
    if (e.kind !== 'material') continue;
    const title = str(c.material_title);
    if (e.title === undefined && title !== undefined) e.title = title;
    const page = pageOf(c.page);
    if (page !== undefined) e.pages.push(page);
  }
  return entries.map((e): Reference => {
    if (e.kind === 'url') return formatCitation(e.citation);
    const title = e.title ?? titles.get(e.id) ?? `source ${e.id.slice(0, 8)}`;
    const distinct = new Set(e.pages).size;
    if (distinct === 0) return { kind: 'text', text: title };
    return { kind: 'text', text: `${title}, ${distinct === 1 ? 'p.' : 'pp.'} ${pageRanges(e.pages)}` };
  });
};

/** Every citation of a cell's `metadata.reader`, grouped (FR-NOTEBOOK-2). */
export const collectCitations = (reader: Record<string, unknown>, titles: Titles = new Map()): Reference[] =>
  groupCitations(cellCitations(reader), titles);
