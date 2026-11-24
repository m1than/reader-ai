// The link rule (WEB-FRESH FR-FRESHUI-6, INV-FRESHUI-1): a report URL becomes an `href` only when it parses under the
// URL Standard as an absolute http(s) URL without userinfo and its parsed host serialises to the report's `host`
// (the server's ASCII host). The href is the report's own string; anything else is shown as text.

/** Attributes of every anchor made from report data. */
export const EXTERNAL = { rel: 'noopener noreferrer nofollow', target: '_blank' } as const;

export const safeHref = (url: string, host: string): string | null => {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
  if (parsed.username !== '' || parsed.password !== '') return null;
  if (parsed.hostname === '' || parsed.hostname !== host) return null;
  return url;
};

const LESSON_ID = /^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$/;
const CELL_ID = /^[A-Za-z0-9._-]{1,128}$/;

/** The app link for a flagged claim's page (FR-FRESHUI-5): the lesson cell citing it when the report names one with
 * valid ids, else that page's spans view (SRV-INGEST). Built by code; never an external link. */
export const pageHref = (materialId: string, page: number, cell: { lesson_id: string; cell_id: string } | null): string =>
  cell && LESSON_ID.test(cell.lesson_id) && cell.lesson_id !== '.' && cell.lesson_id !== '..' && CELL_ID.test(cell.cell_id)
    ? `/lessons/${cell.lesson_id}#cell-${cell.cell_id}`
    : `/api/materials/${encodeURIComponent(materialId)}/spans?page=${page}`;
