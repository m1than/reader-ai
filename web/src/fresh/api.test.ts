// @trace FR-FRESHUI-1 VEC-FRESHUI-1
// The freshness client: routes, run-time validation of every VEC-FRESHUI-1 body, error bodies kept for the card.
import { describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

interface State {
  material_id: string;
  expect: string[];
  body: Record<string, unknown>;
}
interface Bodies {
  states: Record<string, State>;
  errors: Record<string, { status: number; body: Record<string, unknown> }>;
  post_accepted: { status: number; body: unknown };
  bibliography_saved: Record<string, unknown>;
}
const bodies = () => readJson<Bodies>('spec/fixtures/WEB-FRESH/bodies.json');

const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { 'Content-Type': 'application/json' } });

const client = async (fetchFn: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>, onUnauthorized = vi.fn()) => {
  const { createFreshApi } = await load(() => import('./api'));
  expect(createFreshApi).toBeTypeOf('function');
  return createFreshApi({ fetch: fetchFn as typeof fetch, onUnauthorized });
};

describe('VEC-FRESHUI-1 bodies', () => {
  it('covers every card state named by the spec', () => {
    expect(Object.keys(bodies().states).sort()).toEqual(
      ['budget_refused', 'current', 'empty', 'estimate', 'failed', 'outdated', 'partly_outdated', 'running', 'scan_only', 'stale'].sort(),
    );
    const outdated = bodies().states.outdated!.body.report as { recommendations: unknown[] };
    expect(outdated.recommendations).toHaveLength(5);
  });

  it('every state body validates and keeps its money strings as sent', async () => {
    for (const [name, state] of Object.entries(bodies().states)) {
      const api = await client(async () => json(state.body));
      const got = await api.get(state.material_id);
      expect(got.estimate, name).toEqual(state.body.estimate);
      expect(got.report?.status ?? null, name).toBe((state.body.report as { status?: string } | null)?.status ?? null);
    }
  });
});

describe('freshness client (FR-FRESHUI-1)', () => {
  it('GET, PUT and POST go to the SRV-FRESH routes with the escaped material id', async () => {
    const calls: Array<[string, string, unknown]> = [];
    const b = bodies();
    const api = await client(async (input, init) => {
      const method = init?.method ?? 'GET';
      calls.push([method, String(input), init?.body === undefined ? undefined : JSON.parse(String(init.body))]);
      if (method === 'GET') return json(b.states.empty!.body);
      if (method === 'PUT') return json(b.bibliography_saved);
      return json(b.post_accepted.body, 202);
    });
    await api.get('a/b');
    const saved = await api.saveBibliography('m1', { title: 'T', authors: ['A'] });
    expect(saved.sha256).toBe(b.bibliography_saved.sha256);
    expect(await api.run('m1', { high_usd: '0.39', bibliography_sha256: 'c'.repeat(64) })).toEqual({ report_id: 'a0000000-0000-4000-8000-00000000aa01' });
    expect(calls).toEqual([
      ['GET', '/api/materials/a%2Fb/freshness', undefined],
      ['PUT', '/api/materials/m1/bibliography', { title: 'T', authors: ['A'] }],
      ['POST', '/api/materials/m1/freshness', { high_usd: '0.39', bibliography_sha256: 'c'.repeat(64) }],
    ]);
  });

  it('an invalid body is a bad_response error', async () => {
    for (const bad of [{}, { report: null }, { ...bodies().states.empty!.body, estimate: { low_usd: 0.27, high_usd: 0.39, cap_usd: 1 } }]) {
      const api = await client(async () => json(bad));
      await expect(api.get('m1')).rejects.toMatchObject({ code: 'bad_response' });
    }
    const report = bodies().states.current!.body;
    const api = await client(async () => json({ ...report, report: { ...(report.report as object), verdict: 'fine' } }));
    await expect(api.get('m1')).rejects.toMatchObject({ code: 'bad_response' });
  });

  it('error bodies reach the caller with their code and fields; 401 calls the sign-in hook', async () => {
    const e = bodies().errors.estimate_changed!;
    const api = await client(async () => json(e.body, e.status));
    await expect(api.run('m1', { high_usd: '0.39' })).rejects.toMatchObject({ status: 409, code: 'estimate_changed', body: e.body });
    const onUnauthorized = vi.fn();
    const api401 = await client(async () => json({ error: 'unauthenticated' }, 401), onUnauthorized);
    await expect(api401.get('m1')).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});
