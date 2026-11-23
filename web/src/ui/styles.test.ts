// @trace FR-UI-10
// The app stylesheets use Calm tokens, the library's spacing steps and three radii only (WEB-UI FR-UI-10, V/WEB-polish;
// design pass 2026-11-23: every .css under web/src, radii, motion, the 50rem cells container, KaTeX's declared
// exception).
import { readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { describe, expect, it } from 'vitest';
import { readText, repoRoot } from '../app/testing/fixtures';

const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\//g, '');
const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
// Every stylesheet of the app (the generated calm.css is token data, FR-UI-2, and is checked by its own tests).
const sheets = () =>
  walk(join(repoRoot, 'web/src'))
    .filter((f) => f.endsWith('.css') && !f.endsWith('tokens/calm.css'))
    .map((f) => relative(repoRoot, f));
const app = () => strip(readText('web/src/ui/app.css'));
const all = () => sheets().map((f) => ({ file: f, css: strip(readText(f)) }));
// Innermost rules: `selector { declarations }`, also inside @media blocks.
const rules = (css: string) =>
  [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selector: m[1]!.trim(),
    decls: [...m[2]!.matchAll(/([a-z-]+)\s*:\s*([^;]+);?/g)].map((d) => ({ prop: d[1]!, value: d[2]!.trim() })),
  }));
const declarations = () =>
  all().flatMap(({ file, css }) => rules(css).flatMap((r) => r.decls.map((d) => ({ ...d, file, selector: r.selector }))));

const COLOUR_PROPS =
  /^(color|background|background-color|border|border-(top|right|bottom|left|block|inline)(-(start|end))?(-color)?|border-color|outline|outline-color|box-shadow|fill|caret-color|text-decoration-color)$/;
// Words that may appear in those values besides token colours: widths, styles, keywords and lengths.
const NON_COLOUR = /^(none|0|inherit|transparent|currentColor|solid|dashed|dotted|double|inset|auto|thin|medium|thick|-?\d*\.?\d+(px|rem|em)?)$/;
// KaTeX's inline box (WEB-RENDER FR-RENDER-3) is the one declared exception to the spacing steps.
const KATEX_INLINE = /\.katex\b/;

describe('the app stylesheets (FR-UI-10)', () => {
  it('scans more than app.css', () => {
    expect(sheets()).toContain('web/src/ui/app.css');
    expect(sheets()).toContain('web/src/import/plan/plan.css');
  });

  it('have no literal colours, color-mix included', () => {
    for (const { file, css } of all()) {
      expect(css, file).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(css, file).not.toMatch(/\b(rgba?|hsla?|oklch|oklab|lab|lch|hwb|color-mix|color)\(/);
    }
    const found = declarations().filter((x) => COLOUR_PROPS.test(x.prop));
    expect(found.length).toBeGreaterThan(5);
    for (const d of found) {
      const words = d.value
        .replace(/var\(--(acme|shiki)-[a-z0-9-]+\)/g, '')
        .replace(/,/g, ' ')
        .split(/\s+/)
        .filter(Boolean);
      for (const w of words) expect(w, `${d.file} ${d.prop}: ${d.value}`).toMatch(NON_COLOUR);
    }
  });

  it('space and offset with 0, auto and the library steps only (KaTeX inline box excepted)', () => {
    const step = /^(0|auto|var\(--acme-space-[1-7]\))$/;
    const spacing = /^(padding|margin|gap|row-gap|column-gap|inset|top|right|bottom|left)(-(top|right|bottom|left|block|inline)(-(start|end))?)?$/;
    const found = declarations().filter((d) => spacing.test(d.prop));
    expect(found.length).toBeGreaterThan(10);
    for (const d of found) {
      if (KATEX_INLINE.test(d.selector) && /^-?0\.4em$/.test(d.value)) continue;
      for (const part of d.value.split(/\s+/)) expect(part, `${d.file} ${d.selector} ${d.prop}: ${d.value}`).toMatch(step);
    }
  });

  it('declare the three radii once and use only them', () => {
    expect(app()).toMatch(/--radius-control:\s*6px/);
    expect(app()).toMatch(/--radius-block:\s*8px/);
    expect(app()).toMatch(/--radius-panel:\s*12px/);
    const radii = declarations().filter((d) => /^border(-[a-z]+)*-radius$/.test(d.prop));
    expect(radii.length).toBeGreaterThan(5);
    for (const d of radii)
      for (const part of d.value.split(/\s+/))
        expect(part, `${d.file} ${d.selector} ${d.prop}: ${d.value}`).toMatch(/^(0|50%|999px|var\(--radius-(control|block|panel)\))$/);
  });

  it('give library buttons the control radius and the 28px and 32px heights', () => {
    const button = rules(app()).filter((r) => /\.acme-button/.test(r.selector));
    const has = (prop: string, value: RegExp, sel: RegExp) =>
      button.some((r) => sel.test(r.selector) && r.decls.some((d) => d.prop === prop && value.test(d.value)));
    expect(has('--acme-radius', /^var\(--radius-control\)$/, /\.acme-button/)).toBe(true);
    expect(has('--height', /^28px$/, /data-size='sm'|data-size="sm"/)).toBe(true);
    expect(has('--height', /^32px$/, /^\.acme-button$|data-size='md'|data-size="md"/)).toBe(true);
  });

  it('time transitions with token expressions only (0 under reduced motion)', () => {
    const motion = declarations().filter((d) => /^transition(-duration)?$/.test(d.prop));
    expect(motion.length).toBeGreaterThan(0);
    for (const d of motion) {
      expect(d.value, `${d.file} ${d.selector}`).not.toMatch(/\d(ms|s)\b/);
      expect(d.value, `${d.file} ${d.selector}`).toMatch(/var\(--acme-duration/);
    }
  });

  it('cap the reading column at 38rem and the cells at 50rem, and outline focus with the focus token', () => {
    expect(app()).toMatch(/max-width:\s*38rem/);
    expect(app()).toMatch(/max-width:\s*50rem/);
    expect(app()).toMatch(/:focus-visible[^{]*\{[^}]*outline:\s*2px solid var\(--acme-focus\)/);
  });

  it('set lining figures on headings, hyphenation on prose and break-word (never anywhere) on table cells', () => {
    const r = rules(app());
    const where = (prop: string, value: RegExp) => r.filter((x) => x.decls.some((d) => d.prop === prop && value.test(d.value)));
    expect(where('font-variant-numeric', /lining-nums/).some((x) => /\bh1\b/.test(x.selector))).toBe(true);
    expect(where('hyphens', /^auto$/).some((x) => /\btd\b/.test(x.selector))).toBe(true);
    expect(where('hyphens', /^auto$/).some((x) => /\bp\b/.test(x.selector))).toBe(true);
    expect(where('overflow-wrap', /^break-word$/).some((x) => /\btable\b|\btd\b/.test(x.selector))).toBe(true);
    expect(app()).not.toMatch(/overflow-wrap:\s*anywhere/);
  });
});
