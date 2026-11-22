// Citations as references (WEB-NOTEBOOK FR-NOTEBOOK-2). Span form `{span, page?, material_title?}` (SRV-LESSON) or
// `{url, retrieved}`. Only http and https URLs become links; anything else is shown as text.

export type Citation = { span?: unknown; page?: unknown; material_title?: unknown; url?: unknown; retrieved?: unknown };
export type Reference =
  | { kind: 'text'; text: string; retrieved?: string }
  | { kind: 'link'; href: string; text: string; retrieved: string };

const str = (v: unknown): string | undefined => (typeof v === 'string' && v.length > 0 ? v : undefined);

export const formatCitation = (c: Citation): Reference => {
  const url = str(c.url);
  if (url !== undefined) {
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
  }
  const span = str(c.span) ?? '';
  const title = str(c.material_title);
  const page = typeof c.page === 'number' && Number.isFinite(c.page) ? c.page : undefined;
  if (title && page !== undefined) return { kind: 'text', text: `${title}, p. ${page}` };
  if (title) return { kind: 'text', text: title };
  return { kind: 'text', text: span };
};

const key = (c: Citation): string => (str(c.url) !== undefined ? `url:${String(c.url)}` : `span:${String(c.span)}`);

const asList = (v: unknown): Citation[] => (Array.isArray(v) ? (v.filter((x) => x && typeof x === 'object') as Citation[]) : []);

/** Every citation of a cell's `metadata.reader`, deduplicated by span or URL, in order of first use. */
export const collectCitations = (reader: Record<string, unknown>): Reference[] => {
  const claims = Array.isArray(reader.claims) ? (reader.claims as Array<{ citations?: unknown }>) : [];
  const all = [
    ...claims.flatMap((claim) => asList(claim?.citations)),
    ...asList(reader.citations),
    ...asList(reader.source_refs),
    ...(reader.source_ref && typeof reader.source_ref === 'object' ? [reader.source_ref as Citation] : []),
  ];
  const seen = new Set<string>();
  return all.flatMap((c) => {
    const k = key(c);
    if (seen.has(k)) return [];
    seen.add(k);
    return [formatCitation(c)];
  });
};
