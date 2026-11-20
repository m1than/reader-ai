#!/usr/bin/env node
// Calm draft: checks WCAG 2.2 contrast of calm.tokens.json, including the tinted backgrounds the library
// composes, and generates calm.css from it.
//   node calm.mjs            prints every pair, writes calm.css and ../results/contrast.json, exit 1 on any FAIL
// Text needs 4.5:1 (SC 1.4.3); control borders, focus and accent fills need 3:1 against what they sit on (SC 1.4.11).
//
// How backgrounds are composed, as the library's CSS does it (ui-builder-lib-with-agents, web3/material-parity):
// - color-mix(in <space>, X p%, transparent) is X with alpha p (CSS Color 5: the space does not matter when
//   mixing with transparent), then alpha-composited over what is below, in gamma-encoded sRGB, as browsers do.
// - color-mix(in oklab, X p%, Y) opaque: interpolated in Oklab (Ottosson's matrices), converted back to sRGB.
// - color-mix(in srgb, X p%, Y) opaque: channel-wise in gamma-encoded sRGB.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const tokens = JSON.parse(readFileSync(join(here, 'calm.tokens.json'), 'utf8'));

// --- colour math -------------------------------------------------------------------------------
const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
const hexOf = (c) => `#${c.map((v) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, '0')).join('')}`;
const toLinear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const toGamma = (c) => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055);
const luminance = (hex) => {
  const [r, g, b] = rgb(hex).map(toLinear);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const ratio = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
const toOklab = (hex) => {
  const [r, g, b] = rgb(hex).map(toLinear);
  const [l, m, s] = [
    0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
    0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
    0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
  ].map(Math.cbrt);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
};
const fromOklab = ([L, a, b]) => {
  const [l, m, s] = [L + 0.3963377774 * a + 0.2158037573 * b, L - 0.1055613458 * a - 0.0638541728 * b, L - 0.0894841775 * a - 1.291485548 * b].map((v) => v ** 3);
  return hexOf([
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ].map(toGamma));
};
/** color-mix(in oklab, x p, y), opaque */
const oklabMix = (x, p, y) => { const [a, b] = [toOklab(x), toOklab(y)]; return fromOklab(a.map((v, i) => v * p + b[i] * (1 - p))); };
/** color-mix(in srgb, x p, y), opaque */
const srgbMix = (x, p, y) => { const [a, b] = [rgb(x), rgb(y)]; return hexOf(a.map((v, i) => v * p + b[i] * (1 - p))); };
/** color-mix(in <any>, x p, transparent) composited over `below` */
const over = (x, p, below) => srgbMix(x, p, below);

// --- pairs -------------------------------------------------------------------------------------
const SURFACES = ['canvas', 'surface', 'surface-raised'];
const STATUS = ['success', 'warning', 'danger', 'info'];
const TONE_FILLS = ['accent', 'neutral', 'danger', 'success', 'warning', 'info'];
const BUTTON_TONES = ['accent', 'neutral', 'danger'];
const CHIP_TONES = ['accent', 'danger', 'success', 'warning', 'info'];
const TEXT = 4.5;
const UI = 3;

/** Every pair: what is read (fg), what it sits on (bg, computed per mode), and the minimum. */
const pairs = (t) => [
  // Flat pairs: text, muted text, accent and status colours as text; control borders, focus, accent as UI.
  ...['text', 'text-muted', 'accent', ...STATUS].flatMap((fg) => SURFACES.map((s) => ({ group: 'flat', fg, bg: s, fgHex: t(fg), bgHex: t(s), min: TEXT }))),
  ...['control-border', 'focus', 'accent'].flatMap((fg) => SURFACES.map((s) => ({ group: 'flat', fg, bg: s, fgHex: t(fg), bgHex: t(s), min: UI }))),
  // On-tone text on every tone fill (solid button, pressed chip, filled badges).
  ...TONE_FILLS.map((tone) => ({ group: 'on-tone', fg: 'on-tone', bg: tone, fgHex: t('on-tone'), bgHex: t(tone), min: TEXT })),
  // Soft button: tone text on the tone at 16 % (24 % on hover) over the surface below (button.css:41-42).
  ...BUTTON_TONES.flatMap((tone) => SURFACES.flatMap((s) => [0.16, 0.24].map((p) => ({
    group: 'soft-button', fg: tone, bg: `${tone} ${p * 100}% over ${s}`, fgHex: t(tone), bgHex: over(t(tone), p, t(s)), min: TEXT })))),
  // Outline button hover: tone text on oklab(tone 10 %, glass); glass is the surface under Calm (button.css:45).
  ...BUTTON_TONES.map((tone) => ({ group: 'outline-hover', fg: tone, bg: `oklab(${tone} 10%, glass)`, fgHex: t(tone), bgHex: oklabMix(t(tone), 0.1, t('surface')), min: TEXT })),
  // Ghost button hover: tone text on the 5 % text overlay (--acme-hover) over each surface.
  ...BUTTON_TONES.flatMap((tone) => SURFACES.map((s) => ({ group: 'ghost-hover', fg: tone, bg: `hover over ${s}`, fgHex: t(tone), bgHex: over(t('text'), 0.05, t(s)), min: TEXT }))),
  // Hover overlay under control borders (fields, outline controls).
  ...SURFACES.map((s) => ({ group: 'hover-border', fg: 'control-border', bg: `hover over ${s}`, fgHex: t('control-border'), bgHex: over(t('text'), 0.05, t(s)), min: UI })),
  // Toast: text and muted text on srgb(status 10 %, raised); role=alert at 18 % (toast.css:46, :87).
  ...[...STATUS, 'neutral'].flatMap((status) => [0.1, 0.18].flatMap((p) => ['text', 'text-muted'].map((fg) => ({
    group: 'toast', fg, bg: `srgb(${status} ${p * 100}%, raised)`, fgHex: t(fg), bgHex: srgbMix(t(status), p, t('surface-raised')), min: TEXT })))),
  // Chip: text on oklab(tone 12 %, raised) (solid) and on the tone at 10 % over each surface (soft) (chip.css:39, :43).
  ...CHIP_TONES.flatMap((tone) => [
    { group: 'chip', fg: 'text', bg: `oklab(${tone} 12%, raised)`, fgHex: t('text'), bgHex: oklabMix(t(tone), 0.12, t('surface-raised')), min: TEXT },
    ...SURFACES.map((s) => ({ group: 'chip', fg: 'text', bg: `${tone} 10% over ${s}`, fgHex: t('text'), bgHex: over(t(tone), 0.1, t(s)), min: TEXT })),
  ]),
];

const results = Object.entries(tokens)
  .filter(([mode]) => !mode.startsWith('$'))
  .flatMap(([mode, set]) => pairs((name) => set[name].$value).map(({ group, fg, bg, fgHex, bgHex, min }) => {
    const value = ratio(fgHex, bgHex);
    return { mode, group, fg, bg, fgHex, bgHex, min, ratio: Math.round(value * 100) / 100, pass: value >= min };
  }));

results.forEach(({ mode, group, fg, bg, ratio: r, min, pass }) =>
  console.log(`${pass ? 'PASS' : 'FAIL'} ${mode.padEnd(5)} ${group.padEnd(13)} ${fg.padEnd(14)} on ${bg.padEnd(30)} ${r.toFixed(2)}:1 (min ${min})`));

// --- generated CSS -------------------------------------------------------------------------------
// Unlayered, so it wins over the library's @layer acme.tokens. Calm removes gradients, glass and glow by
// mapping those tokens to flat values. --acme-edge (a 1 px inset highlight) is not overridden.
const vars = (set) => Object.entries(set).map(([name, { $value }]) => `  --acme-${name}: ${$value};`).join('\n');
const flat = (set) => [
  `  --acme-holo: ${set.accent.$value};`,
  `  --acme-holo-soft: color-mix(in srgb, ${set.accent.$value} 12%, transparent);`,
  `  --acme-glass: ${set.surface.$value};`,
  '  --acme-glass-blur: 0px;',
  '  --acme-glow: none;',
  `  --acme-hover: color-mix(in srgb, ${set.text.$value} 5%, transparent);`,
  `  --acme-pressed: color-mix(in srgb, ${set.text.$value} 9%, transparent);`,
  `  --acme-backdrop: color-mix(in srgb, ${set.canvas.$value} 60%, transparent);`,
  '  --acme-shadow: 0 1px 2px rgb(0 0 0 / 0.08), 0 8px 24px rgb(0 0 0 / 0.08);',
].join('\n');
const indent = (text) => text.split('\n').map((line) => `  ${line}`).join('\n');
const css = [
  '/* Generated by calm.mjs from calm.tokens.json. Do not edit. */',
  `:root {\n  color-scheme: dark;\n${vars(tokens.dark)}\n${flat(tokens.dark)}\n}`,
  `:root[data-theme='light'] {\n  color-scheme: light;\n${vars(tokens.light)}\n${flat(tokens.light)}\n}`,
  `@media (prefers-color-scheme: light) {\n  :root:not([data-theme='dark']) {\n    color-scheme: light;\n${indent(vars(tokens.light))}\n${indent(flat(tokens.light))}\n  }\n}`,
  '@media (prefers-reduced-motion: reduce) {\n  :root { --acme-duration: 0ms; --acme-duration-slow: 0ms; }\n}',
  '',
].join('\n\n');
writeFileSync(join(here, 'calm.css'), css);
mkdirSync(join(here, '..', 'results'), { recursive: true });
writeFileSync(join(here, '..', 'results', 'contrast.json'), `${JSON.stringify(results, null, 2)}\n`);

const failed = results.filter(({ pass }) => !pass);
console.log(`\n${results.length - failed.length} of ${results.length} pairs pass`);
process.exit(failed.length ? 1 : 0);
