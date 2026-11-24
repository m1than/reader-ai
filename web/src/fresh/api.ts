// Freshness routes client (SRV-FRESH FR-FRESH-11; WEB-FRESH FR-FRESHUI-1): same-origin requests, every response
// validated at run time (WEB-UI's client pattern). Error bodies are kept whole: 409 estimate_changed carries the new
// estimate, daily_budget_exhausted the server's numbers, 400 bibliography_invalid the field (VEC-FRESHUI-1).
import * as v from 'valibot';
import { ApiError } from '../app/api';

const str = v.string();
const optStr = v.optional(v.nullable(str));
const optInt = v.optional(v.nullable(v.pipe(v.number(), v.integer())));
const money = v.pipe(str, v.regex(/^\d+(\.\d+)?$/));

const BibFields = {
  title: str,
  authors: v.array(str),
  edition: optStr,
  year: optInt,
  publisher: optStr,
  isbn: optStr,
  original_title: optStr,
  original_authors: v.optional(v.nullable(v.array(str))),
  original_edition: optStr,
  original_year: optInt,
};
const Bibliography = v.object({ ...BibFields, source: v.picklist(['owner', 'scan']), sha256: str });
const ScanBibliography = v.object({ ...BibFields, sha256: str });
const Estimate = v.object({ low_usd: money, high_usd: money, cap_usd: money });
const Link = v.object({ url: str, host: str, title: str });
const Page = v.pipe(v.number(), v.integer(), v.minValue(1));

const Report = v.object({
  id: str,
  status: v.picklist(['running', 'done', 'failed', 'budget_refused']),
  created_at: str,
  finished_at: v.nullable(str),
  error: v.nullable(str),
  progress: v.nullable(
    v.object({ stage: v.picklist(['scan', 'search', 'report']), done: v.pipe(v.number(), v.integer()), total: v.pipe(v.number(), v.integer()) }),
  ),
  verdict: v.nullable(v.picklist(['current', 'partly_outdated', 'outdated'])),
  reasons: v.nullable(v.array(v.object({ text: str, citations: v.array(Link) }))),
  claims: v.nullable(
    v.array(
      v.object({
        id: str,
        subject: str,
        kind: v.picklist(['version', 'api', 'protocol', 'standard', 'tool', 'statistic', 'law', 'other']),
        confidence: v.picklist(['low', 'medium', 'high']),
        page: Page,
        span: str,
        cell: v.nullable(v.object({ lesson_id: str, cell_id: str })),
      }),
    ),
  ),
  claim_status: v.nullable(
    v.array(v.object({ id: str, status: v.picklist(['changed', 'unchanged', 'unknown']), note: str, citations: v.array(Link) })),
  ),
  recommendations: v.nullable(
    v.array(
      v.object({
        ...Link.entries,
        date: v.nullable(str),
        date_source: v.literal('model'),
        kind: v.picklist(['book_edition', 'book', 'official_docs', 'course', 'other']),
        why: str,
        covers: v.array(v.number()),
      }),
    ),
  ),
  topics: v.nullable(v.array(v.object({ id: v.number(), title: str }))),
  dropped: v.nullable(v.record(str, v.number())),
});

const Freshness = v.object({
  report: v.nullable(Report),
  stale: v.boolean(),
  stale_reason: v.nullable(v.picklist(['ttl', 'reingested', 'bibliography_changed'])),
  bibliography: v.nullable(Bibliography),
  scan_bibliography: v.nullable(ScanBibliography),
  estimate: Estimate,
  daily_cap_usd: money,
});

const Accepted = v.object({ report_id: str });

export type Freshness = v.InferOutput<typeof Freshness>;
export type Report = v.InferOutput<typeof Report>;
export type Estimate = v.InferOutput<typeof Estimate>;
export type Bibliography = v.InferOutput<typeof Bibliography>;
export type LinkRef = v.InferOutput<typeof Link>;

/** The PUT body: FR-FRESH-1's fields. */
export interface BibliographyInput {
  title: string;
  authors: string[];
  edition?: string | null;
  year?: number | null;
  publisher?: string | null;
  isbn?: string | null;
  original_title?: string | null;
  original_authors?: string[] | null;
  original_edition?: string | null;
  original_year?: number | null;
}

export interface RunBody {
  high_usd: string;
  bibliography_sha256?: string;
}

/** A server refusal with its whole JSON body (VEC-FRESHUI-1 `errors`). */
export class FreshError extends ApiError {
  constructor(
    status: number,
    code: string,
    readonly body: Record<string, unknown>,
  ) {
    super(status, code);
    this.name = 'FreshError';
  }
}

export interface FreshApi {
  get(materialId: string): Promise<Freshness>;
  saveBibliography(materialId: string, body: BibliographyInput): Promise<Bibliography>;
  run(materialId: string, body: RunBody): Promise<{ report_id: string }>;
}

export interface FreshApiDeps {
  fetch: typeof fetch;
  onUnauthorized: () => void;
}

const base = (materialId: string) => `/api/materials/${encodeURIComponent(materialId)}`;

export const createFreshApi = (deps: FreshApiDeps): FreshApi => {
  const call = async <T>(method: 'GET' | 'PUT' | 'POST', path: string, schema: v.GenericSchema<unknown, T>, body?: unknown): Promise<T> => {
    const init: RequestInit = { method, credentials: 'same-origin' };
    if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers = { 'Content-Type': 'application/json' };
    }
    let res: Response;
    try {
      res = await deps.fetch(path, init);
    } catch {
      throw new ApiError(0, 'network');
    }
    if (res.status === 401) {
      deps.onUnauthorized();
      throw new ApiError(401, 'unauthenticated');
    }
    if (!res.ok) {
      const err: unknown = await res.json().catch(() => null);
      const obj = err !== null && typeof err === 'object' && !Array.isArray(err) ? (err as Record<string, unknown>) : {};
      throw new FreshError(res.status, typeof obj.error === 'string' ? obj.error : `http_${res.status}`, obj);
    }
    const parsed = v.safeParse(schema, await res.json().catch(() => undefined));
    if (!parsed.success) throw new ApiError(res.status, 'bad_response');
    return parsed.output;
  };

  return {
    get: (materialId) => call('GET', `${base(materialId)}/freshness`, Freshness),
    saveBibliography: (materialId, body) => call('PUT', `${base(materialId)}/bibliography`, Bibliography, body),
    run: (materialId, body) => call('POST', `${base(materialId)}/freshness`, Accepted, body),
  };
};

/** The estimate inside a 409 estimate_changed body, if it is well formed. */
export const estimateOf = (body: unknown): Estimate | null => {
  const parsed = v.safeParse(Estimate, body);
  return parsed.success ? parsed.output : null;
};
