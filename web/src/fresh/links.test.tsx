// @trace FR-FRESHUI-6 INV-FRESHUI-1
// The link rule: an anchor only for an http(s) URL whose parsed host is the report's `host`; every anchor from report
// data opens in a new tab with noopener noreferrer nofollow.
import { act, cleanup, render } from '@testing-library/react';
import fc from 'fast-check';
import { afterEach, describe, expect, it } from 'vitest';
import { readJson } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

const bodies = () => readJson<{ states: Record<string, { material_id: string; body: Record<string, unknown> }> }>('spec/fixtures/WEB-FRESH/bodies.json');

describe('safeHref (FR-FRESHUI-6)', () => {
  it('returns the report URL string only for http(s) with a matching host', async () => {
    const { safeHref } = await load(() => import('./links'));
    expect(safeHref).toBeTypeOf('function');
    expect(safeHref('https://docs.example/a?b=1#c', 'docs.example')).toBe('https://docs.example/a?b=1#c');
    expect(safeHref('http://docs.example/', 'docs.example')).toBe('http://docs.example/');
    expect(safeHref('https://Docs.Example/x', 'docs.example')).toBe('https://Docs.Example/x');
    // IDN: the URL Standard serialises the host to ASCII; the href stays the report's string.
    expect(safeHref('https://bücher.example/x', 'xn--bcher-kva.example')).toBe('https://bücher.example/x');
    for (const [url, host] of [
      ['https://docs.example/', 'other.example'],
      ['https://docs.example.evil.example/', 'docs.example'],
      ['javascript:alert(1)', 'docs.example'],
      ['data:text/html,<b>x</b>', 'docs.example'],
      ['ftp://docs.example/', 'docs.example'],
      ['https://user:pw@docs.example/', 'docs.example'],
      ['https://user@docs.example/', 'docs.example'],
      ['//docs.example/', 'docs.example'],
      ['/relative', 'docs.example'],
      ['https://[::1/', '::1'],
      ['not a url', 'docs.example'],
      ['https://bücher.example/x', 'bücher.example'],
    ] as const)
      expect(safeHref(url, host), `${url} ${host}`).toBeNull();
  });
});

const evil = fc.constantFrom(
  'javascript:alert(1)',
  'JaVaScRiPt:alert(1)',
  'data:text/html,<script>alert(1)</script>',
  'https://user:pw@docs.example/',
  'https://bücher.example/x',
  'https://xn--bcher-kva.example/x',
  'https://docs.example‮/moc.evil',
  'https://[::1/',
  'vbscript:x',
  '<img src=x onerror=alert(1)>',
);
const url = fc.oneof(fc.webUrl({ withQueryParameters: true, withFragments: true }), evil, fc.string());
const host = fc.oneof(fc.domain(), fc.constantFrom('docs.example', 'xn--bcher-kva.example', 'bücher.example', 'evil.example'));
const title = fc.oneof(fc.string(), fc.constantFrom('<a href="javascript:x">t</a>', '‮evil', '<script>x</script>'));
const link = fc.record({ url, host, title });

const report = fc.record({
  reasons: fc.array(fc.record({ text: fc.string(), citations: fc.array(link, { maxLength: 3 }) }), { maxLength: 3 }),
  claim_status: fc.array(link, { maxLength: 3 }),
  recommendations: fc.array(link, { maxLength: 5 }),
});

const ANCHOR_SAFE = new Set(['noopener', 'noreferrer', 'nofollow']);
const APP_LINK = /^\/(api\/materials\/[0-9a-f-]{36}\/spans\?page=[1-9]\d*|lessons\/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}#cell-[A-Za-z0-9._-]+)$/;

describe('every anchor from report data is safe (INV-FRESHUI-1)', () => {
  it('holds for generated reports with javascript:, data:, userinfo, IDN, bidi and markup', async () => {
    const { FreshnessCard } = await load(() => import('./FreshnessCard'));
    expect(FreshnessCard).toBeTypeOf('function');
    const base = bodies().states.outdated!;
    await fc.assert(
      fc.asyncProperty(report, async (r) => {
        const baseReport = base.body.report as Record<string, unknown>;
        const links = [...r.reasons.flatMap((x) => x.citations), ...r.claim_status, ...r.recommendations];
        const body = {
          ...base.body,
          report: {
            ...baseReport,
            reasons: r.reasons,
            claims: r.claim_status.map((_, i) => ({ id: `c${i + 1}`, subject: 's', kind: 'api', confidence: 'low', page: i + 1, span: `${'a'.repeat(64)}:${i}`, cell: null })),
            claim_status: r.claim_status.map((l, i) => ({ id: `c${i + 1}`, status: 'changed', note: 'n', citations: [l] })),
            recommendations: r.recommendations.map((l) => ({ ...l, date: null, date_source: 'model', kind: 'other', why: 'w', covers: [] })),
          },
        };
        const api = { get: async () => body, saveBibliography: async () => base.body.bibliography, run: async () => ({ report_id: 'x' }) };
        let container!: HTMLElement;
        await act(async () => {
          container = render(<FreshnessCard materialId={base.material_id} api={api as never} />).container;
        });
        for (const a of container.querySelectorAll('a')) {
          const href = a.getAttribute('href') ?? '';
          if (APP_LINK.test(href) && a.getAttribute('target') === null) continue;
          const pairs = links.filter((l) => l.url === href);
          expect(pairs.length, href).toBeGreaterThan(0);
          const parsed = new URL(href);
          expect(['http:', 'https:']).toContain(parsed.protocol);
          expect(parsed.username + parsed.password).toBe('');
          expect(pairs.some((l) => l.host === parsed.hostname), href).toBe(true);
          const rel = new Set((a.getAttribute('rel') ?? '').split(/\s+/));
          for (const t of ANCHOR_SAFE) expect(rel.has(t), `${href} rel`).toBe(true);
          expect(a.getAttribute('target')).toBe('_blank');
        }
        cleanup();
      }),
      { numRuns: 25 },
    );
  }, 60_000);
});
