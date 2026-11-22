// @trace INV-UI-1 VEC-UI-1 FR-UI-2 INV-UI-3
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { load } from '../../app/testing/load';
import { readYaml, webRoot } from '../../app/testing/fixtures';

const recipes = () => readYaml('spec/fixtures/WEB-UI/contrast-recipes.yaml');
const tokens = () => JSON.parse(readFileSync(`${webRoot}src/ui/tokens/calm.tokens.json`, 'utf8')) as unknown;

describe('Calm contrast (INV-UI-1, VEC-UI-1)', () => {
  it('expands the recipes to 109 pairs per mode, 218 in all', async () => {
    const { expandPairs } = await load(() => import('./contrast'));
    expect(expandPairs).toBeTypeOf('function');
    const pairs = expandPairs(recipes(), tokens());
    expect(pairs.filter((p) => p.mode === 'light')).toHaveLength(109);
    expect(pairs.filter((p) => p.mode === 'dark')).toHaveLength(109);
  });

  it('meets 4.5:1 for text and 3:1 for UI in every expanded pair', async () => {
    const { expandPairs } = await load(() => import('./contrast'));
    expect(expandPairs).toBeTypeOf('function');
    const failing = expandPairs(recipes(), tokens()).filter((p) => p.ratio < p.min);
    expect(failing).toEqual([]);
  });

  it('computes WCAG ratios and colour mixes correctly on known values', async () => {
    const { contrastRatio, mixOver, mixSrgb, mixOklab } = await load(() => import('./contrast'));
    expect(contrastRatio).toBeTypeOf('function');
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 2);
    expect(contrastRatio('#22211e', '#f6f4ef')).toBeCloseTo(14.65, 2);
    expect(mixOver('#ffffff', 0.5, '#000000')).toBe('#808080');
    expect(mixSrgb('#ff0000', 0.5, '#0000ff')).toBe('#800080');
    expect(mixOklab('#ffffff', 1, '#000000')).toBe('#ffffff');
  });
});

describe('Calm tokens and generated CSS (FR-UI-2, INV-UI-3 tokens)', () => {
  it('has light and dark sets and no gradient value', () => {
    const t = tokens() as Record<string, Record<string, { $value: string }>>;
    expect(Object.keys(t)).toEqual(expect.arrayContaining(['light', 'dark']));
    const values = ['light', 'dark'].flatMap((m) => Object.values(t[m] ?? {}).map((v) => v.$value));
    expect(values.length).toBeGreaterThan(10);
    expect(values.filter((v) => /gradient/i.test(v))).toEqual([]);
  });

  it('generates calm.css deterministically and the committed file equals the output', async () => {
    const { buildCss } = await load(() => import('./build.mjs'));
    expect(buildCss).toBeTypeOf('function');
    const a = buildCss(tokens());
    expect(buildCss(tokens())).toBe(a);
    expect(readFileSync(`${webRoot}src/ui/tokens/calm.css`, 'utf8')).toBe(a);
    expect(a).toContain('--acme-canvas');
    expect(a).toContain('--acme-glass-blur: 0px');
    expect(a).toMatch(/prefers-reduced-motion: reduce[\s\S]*--acme-duration: 0ms/);
    expect(a).not.toMatch(/gradient|backdrop-filter/);
  });
});
