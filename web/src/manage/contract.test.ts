// @trace FR-MANAGE-6 VEC-MANAGE-2 VEC-MANAGE-1
// WEB-MANAGE FR-MANAGE-6: the settings page reads SRV-CONFIG FR-CONFIG-25's body as the server sends it.
// spec/fixtures/WEB-MANAGE/settings-view.json is the route's real answer (server/tests/config/test_settings_db.py
// asserts the route answers exactly its `body`); here the client's validator parses it, and VEC-MANAGE-1's stub rows
// carry exactly its row fields, so neither the client nor the stubs can drift from the server.
import { describe, expect, it, vi } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { createManageApi } from './api';
import { fixture } from './testkit';

type Body = { rows: Array<Record<string, unknown>>; ignored: Array<Record<string, unknown>> };
const contract = () => readJson<{ body: Body }>('spec/fixtures/WEB-MANAGE/settings-view.json').body;
const answering = (body: unknown) => {
  const fetch = vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  return createManageApi({ fetch: fetch as unknown as typeof globalThis.fetch, onUnauthorized: vi.fn() });
};

describe('GET /api/settings contract (FR-MANAGE-6, SRV-CONFIG FR-CONFIG-25)', () => {
  it('parses the route’s real body and keeps every row and ignored override', async () => {
    const body = contract();
    const parsed = await answering(body).settings();
    expect(parsed.rows).toEqual(body.rows);
    expect(parsed.ignored).toEqual(body.ignored);
    expect(parsed.rows.filter((r) => r.key !== null).map((r) => r.key)).toEqual([
      'sharing.signup',
      'observability.retention_days',
      'budgets.monthly_usd',
      'budgets.warn_at',
      'tasks.draft.budget_usd_per_lesson',
      'tasks.tutor.budget_usd_per_lesson',
    ]);
  });

  it('refuses the old `{settings: [...]}` shape as a bad response', async () => {
    await expect(answering({ settings: contract().rows }).settings()).rejects.toMatchObject({ code: 'bad_response' });
  });

  it('VEC-MANAGE-1’s settings stub has the route’s shape and row fields', () => {
    const stub = fixture().settings as Body;
    const fields = (r: Record<string, unknown>) => Object.keys(r).sort();
    const want = fields(contract().rows[0]!);
    expect(Object.keys(stub).sort()).toEqual(['ignored', 'rows']);
    for (const r of stub.rows) expect(fields(r), String(r.name)).toEqual(want);
    expect(fields(fixture().settings_signup_file_off as Record<string, unknown>)).toEqual(want);
    for (const o of stub.ignored) expect(fields(o)).toEqual(fields(contract().ignored[0]!));
  });
});
