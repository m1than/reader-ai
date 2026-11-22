// WCAG 2.2 contrast over the Calm tokens, with backgrounds composed as the library's CSS composes them
// (WEB-UI INV-UI-1, VEC-UI-1). An independent implementation of the spike's calm.mjs colour math:
// - over(x, p, below): color-mix(in <any>, x p, transparent) alpha-composited over `below` in gamma sRGB;
// - mixSrgb(x, p, y): color-mix(in srgb, x p, y), channel-wise in gamma sRGB;
// - mixOklab(x, p, y): color-mix(in oklab, x p, y), interpolated in Oklab (Ottosson's matrices).

type Rgb = [number, number, number];

const rgb = (hex: string): Rgb => [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255) as Rgb;
const hex = (c: readonly number[]): string =>
  `#${c.map((v) => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, '0')).join('')}`;
const linear = (c: number): number => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const gamma = (c: number): number => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055);

const luminance = (h: string): number => {
  const [r, g, b] = rgb(h).map(linear) as Rgb;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

export const contrastRatio = (a: string, b: string): number => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
};

export const mixSrgb = (x: string, p: number, y: string): string => {
  const [a, b] = [rgb(x), rgb(y)];
  return hex(a.map((v, i) => v * p + (b[i] ?? 0) * (1 - p)));
};

export const mixOver = (x: string, p: number, below: string): string => mixSrgb(x, p, below);

const toOklab = (h: string): Rgb => {
  const [r, g, b] = rgb(h).map(linear) as Rgb;
  const [l, m, s] = [
    0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b,
    0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b,
    0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b,
  ].map(Math.cbrt) as Rgb;
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
};

const fromOklab = ([L, a, b]: Rgb): string => {
  const [l, m, s] = [
    L + 0.3963377774 * a + 0.2158037573 * b,
    L - 0.1055613458 * a - 0.0638541728 * b,
    L - 0.0894841775 * a - 1.291485548 * b,
  ].map((v) => v ** 3) as Rgb;
  return hex(
    [
      4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
      -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
      -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
    ].map(gamma),
  );
};

export const mixOklab = (x: string, p: number, y: string): string => {
  const [a, b] = [toOklab(x), toOklab(y)];
  return fromOklab(a.map((v, i) => v * p + (b[i] ?? 0) * (1 - p)) as Rgb);
};

// ---- recipes (VEC-UI-1) --------------------------------------------------------------------------------------------

export interface Recipes {
  sets: Record<string, string[]>;
  groups: Array<Record<string, unknown>>;
}

export interface Pair {
  mode: string;
  group: string;
  fg: string;
  bg: string;
  min: number;
  ratio: number;
}

const MIN: Record<string, number> = { text: 4.5, ui: 3 };

/** Expands `{SET}` and `{var}` placeholders into every combination. */
const values = (spec: unknown, sets: Record<string, string[]>): Array<string | number> => {
  if (Array.isArray(spec)) return spec as Array<string | number>;
  if (typeof spec === 'string') {
    const m = /^\{([A-Z_]+)\}$/.exec(spec);
    if (m) return sets[m[1]!] ?? [];
    return [spec];
  }
  return spec === undefined ? [] : [spec as number];
};

const expandGroup = (group: Record<string, unknown>, sets: Record<string, string[]>): Array<Record<string, string>> => {
  const tones = group.tone === undefined ? [undefined] : values(group.tone, sets);
  const ps = group.p === undefined ? [undefined] : values(group.p, sets);
  const out: Array<Record<string, string>> = [];
  for (const tone of tones) {
    for (const p of ps) {
      const fill = (s: string) =>
        s.replaceAll('{tone}', String(tone)).replaceAll('{p}', String(p));
      const fgs = group.fg === '{tone}' ? [String(tone)] : values(group.fg, sets).map(String);
      const bgSpec = String(group.bg);
      const setRef = /\{([A-Z_]+)\}/.exec(bgSpec);
      const bgs = setRef
        ? (sets[setRef[1]!] ?? []).map((s) => fill(bgSpec.replace(setRef[0], s)))
        : [fill(bgSpec)];
      for (const fg of fgs) for (const bg of bgs) out.push({ fg, bg });
    }
  }
  return out;
};

const resolveBg = (expr: string, t: (name: string) => string): string => {
  const call = /^(over|oklab|srgb)\(\s*([\w-]+)\s*,\s*([\d.]+)\s*,\s*([\w-]+)\s*\)$/.exec(expr);
  if (!call) return t(expr);
  const [, fn, x, p, y] = call as unknown as [string, string, string, string, string];
  const below = y === 'glass' ? t('surface') : t(y);
  const pv = Number(p);
  return fn === 'over' ? mixOver(t(x), pv, below) : fn === 'srgb' ? mixSrgb(t(x), pv, below) : mixOklab(t(x), pv, below);
};

/** Expands VEC-UI-1's recipes over the committed tokens into concrete pairs with their ratios. */
export const expandPairs = (recipes: unknown, tokens: unknown): Pair[] => {
  const r = recipes as Recipes;
  const tk = tokens as Record<string, Record<string, { $value: string }>>;
  return ['light', 'dark'].flatMap((mode) => {
    const set = tk[mode] ?? {};
    const t = (name: string): string => {
      const v = set[name]?.$value;
      if (!v) throw new Error(`token ${mode}.${name} missing`);
      return v;
    };
    return r.groups.flatMap((g) =>
      expandGroup(g, r.sets).map(({ fg, bg }) => ({
        mode,
        group: String(g.group),
        fg: fg!,
        bg: bg!,
        min: MIN[String(g.min)] ?? 4.5,
        ratio: contrastRatio(t(fg!), resolveBg(bg!, t)),
      })),
    );
  });
};
