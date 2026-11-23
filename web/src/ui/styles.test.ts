// @trace FR-UI-10
// The app stylesheet uses Calm tokens and the library's spacing steps only (WEB-UI FR-UI-10, V/WEB-polish).
import { describe, expect, it } from 'vitest';
import { readText } from '../app/testing/fixtures';

const css = () => readText('web/src/ui/app.css').replace(/\/\*[\s\S]*?\*\//g, '');
const declarations = () =>
  [...css().matchAll(/([a-z-]+)\s*:\s*([^;{}]+);/g)].map((m) => ({ prop: m[1]!, value: m[2]!.trim() }));

const COLOUR_PROPS =
  /^(color|background|background-color|border|border-(top|right|bottom|left|block|inline)(-(start|end))?(-color)?|border-color|outline|outline-color|box-shadow|fill|caret-color|text-decoration-color)$/;
// Words that may appear in those values besides token colours: widths, styles, keywords and lengths.
const NON_COLOUR = /^(none|0|inherit|transparent|currentColor|solid|dashed|dotted|double|inset|auto|thin|medium|thick|-?\d*\.?\d+(px|rem|em)?)$/;

describe('app.css (FR-UI-10)', () => {
  it('has no literal colours, color-mix included', () => {
    expect(css()).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(css()).not.toMatch(/\b(rgba?|hsla?|oklch|oklab|lab|lch|hwb|color-mix|color)\(/);
    const found = declarations().filter((x) => COLOUR_PROPS.test(x.prop));
    expect(found.length).toBeGreaterThan(5);
    for (const d of found) {
      const words = d.value.replace(/var\(--acme-[a-z0-9-]+\)/g, '').replace(/,/g, ' ').split(/\s+/).filter(Boolean);
      for (const w of words) expect(w, `${d.prop}: ${d.value}`).toMatch(NON_COLOUR);
    }
  });

  it('spaces and offsets with 0, auto and the library steps only', () => {
    const step = /^(0|auto|var\(--acme-space-[1-7]\))$/;
    const spacing = /^(padding|margin|gap|row-gap|column-gap|inset|top|right|bottom|left)(-(top|right|bottom|left|block|inline)(-(start|end))?)?$/;
    const found = declarations().filter((d) => spacing.test(d.prop));
    expect(found.length).toBeGreaterThan(10);
    for (const d of found) for (const part of d.value.split(/\s+/)) expect(part, `${d.prop}: ${d.value}`).toMatch(step);
  });

  it('caps the reading column at 38rem and outlines focus with the focus token', () => {
    expect(css()).toMatch(/max-width:\s*38rem/);
    expect(css()).toMatch(/:focus-visible[^{]*\{[^}]*outline:\s*2px solid var\(--acme-focus\)/);
  });
});
