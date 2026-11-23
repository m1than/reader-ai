// @trace INV-FRESHUI-2 FR-FRESHUI-7
// Report strings are text nodes only; the card's stylesheet follows WEB-UI FR-UI-10 (tokens and spacing steps only).
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { readJson, readText } from '../app/testing/fixtures';
import { load } from '../app/testing/load';

afterEach(cleanup);

const MARKUP = [
  '<img src=x onerror="alert(1)">',
  '<script>alert(2)</script>',
  '<b style="color:red">bold</b>',
  '" onmouseover="alert(3)',
  '<svg onload=alert(4)>',
  '<style>*{display:none}</style>',
];

/** Replaces every string of a JSON value by markup, except URLs that keep a safe shape and the enum fields. */
const poison = (value: unknown, i = { n: 0 }, key = ''): unknown => {
  if (typeof value === 'string') {
    if (['status', 'verdict', 'kind', 'confidence', 'stage', 'date_source', 'source', 'stale_reason', 'sha256', 'id', 'span', 'created_at', 'finished_at', 'low_usd', 'high_usd', 'cap_usd', 'daily_cap_usd', 'lesson_id', 'cell_id', 'date'].includes(key))
      return value;
    i.n += 1;
    const m = MARKUP[i.n % MARKUP.length]!;
    return key === 'url' ? `https://docs.example/${encodeURIComponent(m)}` : key === 'host' ? 'docs.example' : `${m} ${i.n}`;
  }
  if (Array.isArray(value)) return value.map((v) => poison(v, i, key));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, poison(v, i, k)]));
  return value;
};

const bodies = () => readJson<{ states: Record<string, { material_id: string; body: Record<string, unknown> }> }>('spec/fixtures/WEB-FRESH/bodies.json');

describe('report strings are text only (INV-FRESHUI-2)', () => {
  it('markup and script strings in every field create no element, attribute or style', async () => {
    const { FreshnessCard } = await load(() => import('./FreshnessCard'));
    expect(FreshnessCard).toBeTypeOf('function');
    for (const name of ['partly_outdated', 'outdated', 'scan_only', 'failed']) {
      const s = bodies().states[name]!;
      const body = poison(s.body) as Record<string, unknown>;
      const api = { get: async () => body, saveBibliography: async () => body.bibliography, run: async () => ({ report_id: 'x' }) };
      let container!: HTMLElement;
      await act(async () => {
        container = render(<FreshnessCard materialId={s.material_id} api={api as never} />).container;
      });
      expect(MARKUP.some((m) => container.textContent?.includes(m)), name).toBe(true);
      expect(container.querySelectorAll('img, script, style, iframe, object, embed, b').length, name).toBe(0);
      expect(container.querySelectorAll('svg').length, name).toBe(container.querySelectorAll('.fresh-badge svg').length);
      for (const el of container.querySelectorAll('*')) {
        for (const attr of el.attributes) {
          if (el.tagName === 'A' && attr.name === 'href') continue; // INV-FRESHUI-1 anchors
          expect(attr.name, `${name} ${el.tagName}`).not.toMatch(/^on/);
          expect(attr.value, `${name} ${el.tagName}[${attr.name}]`).not.toMatch(/alert|<|onerror|onload|color:red/);
        }
      }
    }
  });
});

describe('card stylesheet (FR-FRESHUI-7, WEB-UI FR-UI-10)', () => {
  const css = () => readText('web/src/fresh/fresh.css').replace(/\/\*[\s\S]*?\*\//g, '');

  it('uses Calm tokens and the library spacing steps only, and wraps long URLs anywhere', () => {
    const text = css();
    expect(text).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(text).not.toMatch(/\b(rgba?|hsla?|oklch|oklab|lab|lch|hwb|color-mix|color)\(/);
    const step = /^(0|auto|var\(--acme-space-[1-7]\))$/;
    const spacing = /^(padding|margin|gap|row-gap|column-gap|inset|top|right|bottom|left)(-(top|right|bottom|left|block|inline)(-(start|end))?)?$/;
    const decls = [...text.matchAll(/([a-z-]+)\s*:\s*([^;{}]+);/g)].map((m) => ({ prop: m[1]!, value: m[2]!.trim() }));
    expect(decls.length).toBeGreaterThan(5);
    for (const d of decls.filter((x) => spacing.test(x.prop))) for (const part of d.value.split(/\s+/)) expect(part, `${d.prop}: ${d.value}`).toMatch(step);
    expect(text).toMatch(/overflow-wrap:\s*anywhere/);
    expect(text).toMatch(/:focus-visible[^{]*\{[^}]*outline:\s*2px solid var\(--acme-focus\)/);
  });
});
