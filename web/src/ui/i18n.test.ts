// @trace FR-UI-30 FR-UI-31
// The interface language (WEB-UI FR-UI-30, FR-UI-31): catalogues per area as data, their parity, Russian and English
// plurals, the locale choice and the formatting locale, server enums through the owning catalogue, the collator, the
// provider's switch and theme.js's resolver against the runtime's (node:vm).
import { readFileSync } from 'node:fs';
import { createElement, useEffect, type ReactNode } from 'react';
import { runInNewContext } from 'node:vm';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { load } from '../app/testing/load';
import { webRoot } from '../app/testing/fixtures';

afterEach(cleanup);

type Message = string | Record<string, string>;
type Catalogue = Record<string, Message>;
type Lang = 'en' | 'ru';
interface I18nModule {
  I18N_AREAS: readonly string[];
  PLURAL_CATEGORIES: Record<Lang, readonly string[]>;
  LOCALE_KEY: string;
  resolveLang: (stored: string | null | undefined, languages: readonly string[] | undefined) => Lang;
  formatLocale: (lang: Lang, languages: readonly string[] | undefined) => string;
  formatMessage: (lang: Lang, locale: string, message: Message, values?: Record<string, unknown>) => string;
  enumText: (catalogue: Catalogue, name: string, value: string) => string;
  I18nProvider: (p: { children?: ReactNode; languages?: readonly string[] }) => ReactNode;
  useLocale: () => { lang: Lang; choice: string; formatLocale: string; setChoice: (c: 'auto' | 'en' | 'ru') => Promise<void> };
  useT: (area: string, en: Catalogue) => (key: string, values?: Record<string, unknown>) => string;
}
const I18N_PATH = './i18n';
const i18n = () => load<Partial<I18nModule>>(() => import(/* @vite-ignore */ I18N_PATH) as Promise<I18nModule>);

// FR-UI-30: every area's two modules, found by one glob over the source tree.
const modules = import.meta.glob<{ default: Catalogue }>('/src/**/messages.{en,ru}.ts', { eager: true });
const areaOf = (path: string) => path.replace(/^\/src\//, '').replace(/\/messages\.(en|ru)\.ts$/, '');
const catalogues = (lang: Lang): Map<string, Catalogue> =>
  new Map(
    Object.entries(modules)
      .filter(([p]) => p.endsWith(`messages.${lang}.ts`))
      .map(([p, m]) => [areaOf(p), m.default]),
  );
const placeholders = (m: Message): string[] =>
  [...new Set((typeof m === 'string' ? [m] : Object.values(m)).flatMap((s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]!)))].sort();
const isPlural = (m: Message): m is Record<string, string> => typeof m === 'object';

describe('catalogues (FR-UI-30)', () => {
  it('the glob finds exactly the areas of I18N_AREAS, each with an en and a ru module', async () => {
    const { I18N_AREAS } = await i18n();
    expect(I18N_AREAS).toEqual(['app', 'app/observability', 'ui', 'notebook', 'render', 'tutor', 'import', 'import/plan', 'accounts', 'fresh', 'manage']);
    expect([...catalogues('en').keys()].sort()).toEqual([...(I18N_AREAS ?? [])].sort());
    expect([...catalogues('ru').keys()].sort()).toEqual([...(I18N_AREAS ?? [])].sort());
  });

  it('has equal key sets, exact plural categories and equal placeholders in both locales', async () => {
    const { PLURAL_CATEGORIES } = await i18n();
    expect(PLURAL_CATEGORIES).toEqual({ en: ['one', 'other'], ru: ['one', 'few', 'many', 'other'] });
    const en = catalogues('en');
    const ru = catalogues('ru');
    expect(en.size).toBeGreaterThan(0);
    for (const [area, enCat] of en) {
      const ruCat = ru.get(area) ?? {};
      expect(Object.keys(ruCat).sort(), area).toEqual(Object.keys(enCat).sort());
      for (const [key, enMsg] of Object.entries(enCat)) {
        const ruMsg = ruCat[key];
        expect(ruMsg, `${area} ${key}`).toBeDefined();
        if (ruMsg === undefined) continue;
        expect(isPlural(enMsg), `${area} ${key}`).toBe(isPlural(ruMsg));
        if (isPlural(enMsg)) expect(Object.keys(enMsg).sort(), `${area} ${key} en`).toEqual([...PLURAL_CATEGORIES!.en].sort());
        if (isPlural(ruMsg)) expect(Object.keys(ruMsg).sort(), `${area} ${key} ru`).toEqual([...PLURAL_CATEGORIES!.ru].sort());
        expect(placeholders(ruMsg), `${area} ${key}`).toEqual(placeholders(enMsg));
        // Every message is translated: a Russian message is never the English text verbatim, unless it holds no
        // letters at all (a number pattern) or only identifiers.
        if (typeof enMsg === 'string' && /[a-z]{3}/i.test(enMsg.replace(/\{\w+\}/g, '')) && !/^[\w.-]+$/.test(enMsg))
          expect(ruMsg, `${area} ${key} untranslated`).not.toBe(enMsg);
      }
    }
  });
});

describe('plurals and numbers (FR-UI-30)', () => {
  const lessons = { ru: { one: '{count} урок', few: '{count} урока', many: '{count} уроков', other: '{count} урока' }, en: { one: '{count} lesson', other: '{count} lessons' } };
  const ruExpected: Record<number, string> = {
    0: '0 уроков', 1: '1 урок', 2: '2 урока', 5: '5 уроков', 11: '11 уроков', 12: '12 уроков', 21: '21 урок', 22: '22 урока', 111: '111 уроков', 1.5: '1,5 урока',
  };
  const enExpected: Record<number, string> = {
    0: '0 lessons', 1: '1 lesson', 2: '2 lessons', 5: '5 lessons', 11: '11 lessons', 12: '12 lessons', 21: '21 lessons', 22: '22 lessons', 111: '111 lessons', 1.5: '1.5 lessons',
  };
  it.each([0, 1, 2, 5, 11, 12, 21, 22, 111, 1.5])('formats %s in Russian and English', async (n) => {
    const { formatMessage } = await i18n();
    expect(formatMessage?.('ru', 'ru', lessons.ru, { count: n })).toBe(ruExpected[n]);
    expect(formatMessage?.('en', 'en', lessons.en, { count: n })).toBe(enExpected[n]);
  });

  it('fills placeholders as text and formats numbers in the formatting locale', async () => {
    const { formatMessage } = await i18n();
    expect(formatMessage?.('en', 'en', 'Hello {name}, {n} items', { name: '<b>x</b>', n: 1234.5 })).toBe('Hello <b>x</b>, 1,234.5 items');
    expect(formatMessage?.('ru', 'ru', '{n} шт.', { n: 1234.5 })).toBe('1 234,5 шт.');
  });
});

describe('locale choice and formatting locale (FR-UI-30)', () => {
  it('auto, missing and unknown values follow the primary subtag of navigator.languages[0], case-insensitively', async () => {
    const { resolveLang, LOCALE_KEY } = await i18n();
    expect(LOCALE_KEY).toBe('reader-ai.locale');
    expect(resolveLang?.('auto', ['ru-RU', 'en-US'])).toBe('ru');
    expect(resolveLang?.(null, ['RU'])).toBe('ru');
    expect(resolveLang?.('junk', ['ru'])).toBe('ru');
    expect(resolveLang?.('auto', ['en-US', 'ru-RU'])).toBe('en');
    expect(resolveLang?.('auto', ['uk'])).toBe('en');
    expect(resolveLang?.('auto', [])).toBe('en');
    expect(resolveLang?.(undefined, undefined)).toBe('en');
    expect(resolveLang?.('en', ['ru-RU'])).toBe('en');
    expect(resolveLang?.('ru', ['en-US'])).toBe('ru');
  });

  it('uses the full browser tag when its primary subtag is the chosen language, else the bare tag', async () => {
    const { formatLocale } = await i18n();
    expect(formatLocale?.('ru', ['ru-RU'])).toBe('ru-RU');
    expect(formatLocale?.('ru', ['RU'])).toBe('RU');
    expect(formatLocale?.('ru', ['ru'])).toBe('ru');
    expect(formatLocale?.('ru', ['en-GB'])).toBe('ru');
    expect(formatLocale?.('en', ['en-GB'])).toBe('en-GB');
    expect(formatLocale?.('en', ['uk'])).toBe('en');
    expect(formatLocale?.('ru', ['uk'])).toBe('ru');
    expect(formatLocale?.('en', [])).toBe('en');
  });
});

// FR-UI-30 server values: each enum shown to the user maps through its area's catalogue (`enum.<name>.<value>`).
const REQUIRED_ENUMS = [
  'app:stage',
  'notebook:kind',
  'notebook:state',
  'import:upload_state',
  'import/plan:reason',
  'fresh:kind',
  'fresh:confidence',
  'fresh:status',
];
const enumsOf = (): Array<[string, string, string]> => {
  const out = new Map<string, [string, string, string]>();
  for (const [area, cat] of catalogues('en'))
    for (const key of Object.keys(cat)) {
      const m = /^enum\.(\w+)\.(.+)$/.exec(key);
      if (m && !out.has(`${area}:${m[1]}`)) out.set(`${area}:${m[1]}`, [area, m[1]!, m[2]!]);
    }
  return [...out.values()];
};

describe('server enums (FR-UI-30)', () => {
  it('maps the required enums', () => {
    const found = enumsOf().map(([a, n]) => `${a}:${n}`);
    for (const e of REQUIRED_ENUMS) expect(found, e).toContain(e);
  });

  it.each(enumsOf())('%s %s: a known value is translated, an unknown one is shown as received', async (area, name, value) => {
    const { enumText } = await i18n();
    const ru = catalogues('ru').get(area)!;
    const en = catalogues('en').get(area)!;
    expect(enumText?.(en, name, value)).toBe(en[`enum.${name}.${value}`]);
    expect(enumText?.(ru, name, value)).toBe(ru[`enum.${name}.${value}`]);
    expect(enumText?.(ru, name, value)).not.toBe(value);
    expect(enumText?.(ru, name, 'zz_unknown_value')).toBe('zz_unknown_value');
    expect(enumText?.(en, name, 'constructor')).toBe('constructor');
  });
});

describe('collator (FR-UI-30)', () => {
  it('sorts course titles with the chosen locale: Cyrillic first in Russian, Latin first in English', async () => {
    const { groupLessons } = await import('../app/lessons');
    const { formatLocale } = await i18n();
    const l = (course: string) => ({ lesson_id: course, course_id: course, course_title: course, ordinal: 1, title: 't', updated_at: '2026-11-01T00:00:00Z', latest_sha256: null });
    const lessons = [l('Apple'), l('Яблоко')];
    expect(groupLessons(lessons, formatLocale?.('ru', ['ru-RU']) ?? 'x').map((g) => g.course_title)).toEqual(['Яблоко', 'Apple']);
    expect(groupLessons(lessons, formatLocale?.('en', ['ru-RU']) ?? 'x').map((g) => g.course_title)).toEqual(['Apple', 'Яблоко']);
  });
});

describe('runtime and provider (FR-UI-30, FR-UI-31)', () => {
  const en = { hello: 'Lessons', n: { one: '{count} lesson', other: '{count} lessons' } };

  it('renders English with no provider mounted', async () => {
    const { useT } = await i18n();
    expect(useT).toBeTypeOf('function');
    localStorage.setItem('reader-ai.locale', 'ru');
    function Probe() {
      const t = useT!('app', en);
      return createElement('p', null, `${t('hello')} ${t('n', { count: 2 })}`);
    }
    render(createElement(Probe));
    expect(screen.getByText('Lessons 2 lessons')).toBeTruthy();
    localStorage.removeItem('reader-ai.locale');
  });

  it('switches without a reload, updates html[lang], stores the choice and keeps focus', async () => {
    const { I18nProvider, useLocale, useT } = await i18n();
    expect(I18nProvider).toBeTypeOf('function');
    let api: ReturnType<I18nModule['useLocale']> | null = null;
    function Probe() {
      const loc = useLocale!();
      const t = useT!('app', (modules['/src/app/messages.en.ts']?.default ?? {}) as Catalogue);
      useEffect(() => {
        api = loc;
      });
      return createElement('div', null, createElement('h1', null, t('list.title')), createElement('button', { type: 'button' }, 'keep'));
    }
    render(createElement(I18nProvider!, { languages: ['en-US'] }, createElement(Probe)));
    expect(document.documentElement.lang).toBe('en');
    expect(screen.getByRole('heading').textContent).toBe('Lessons');
    const button = screen.getByRole('button', { name: 'keep' });
    button.focus();
    await act(async () => {
      await api!.setChoice('ru');
    });
    await waitFor(() => expect(document.documentElement.lang).toBe('ru'));
    expect(screen.getByRole('heading').textContent).toBe((modules['/src/app/messages.ru.ts']?.default as Catalogue)['list.title']);
    expect(localStorage.getItem('reader-ai.locale')).toBe('ru');
    expect(document.activeElement).toBe(button);
    await act(async () => {
      await api!.setChoice('auto');
    });
    await waitFor(() => expect(document.documentElement.lang).toBe('en'));
    expect(localStorage.getItem('reader-ai.locale')).toBe('auto');
    expect(screen.getByRole('heading').textContent).toBe('Lessons');
  });

  it('starts from the stored choice', async () => {
    const { I18nProvider, useT } = await i18n();
    localStorage.setItem('reader-ai.locale', 'ru');
    const ruTitle = (modules['/src/app/messages.ru.ts']?.default as Catalogue | undefined)?.['list.title'];
    function Probe() {
      const t = useT!('app', (modules['/src/app/messages.en.ts']?.default ?? {}) as Catalogue);
      return createElement('h1', null, t('list.title'));
    }
    render(createElement(I18nProvider!, { languages: ['en-US'] }, createElement(Probe)));
    await waitFor(() => expect(screen.getByRole('heading').textContent).toBe(ruTitle));
    expect(document.documentElement.lang).toBe('ru');
    localStorage.removeItem('reader-ai.locale');
  });
});

describe('Vitest setup pins English (FR-UI-30)', () => {
  it('(a test that leaves a stored choice behind)', () => {
    localStorage.setItem('reader-ai.locale', 'ru');
  });
  it('clears reader-ai.locale before each test', () => {
    expect(localStorage.getItem('reader-ai.locale')).toBeNull();
  });
});

describe('theme.js resolves the locale like the runtime (FR-UI-31)', () => {
  const source = readFileSync(`${webRoot}src/public/theme.js`, 'utf8');
  const run = (stored: string | null, languages: string[]) => {
    const attrs = new Map<string, string>([['lang', 'en']]);
    const root = {
      setAttribute: (k: string, v: string) => void attrs.set(k, v),
      removeAttribute: (k: string) => void attrs.delete(k),
      getAttribute: (k: string) => attrs.get(k) ?? null,
    };
    const storage = { getItem: (k: string) => (k === 'reader-ai.locale' ? stored : null) };
    runInNewContext(source, { document: { documentElement: root }, localStorage: storage, navigator: { languages, language: languages[0] } });
    return attrs.get('lang');
  };
  const STORED = ['auto', 'en', 'ru', 'RU', 'junk', null];
  const LANGS = [['ru-RU'], ['en-US'], ['uk'], []];
  const table = STORED.flatMap((s) => LANGS.map((l) => [s, l] as const));
  it.each(table)('stored %s with navigator.languages %j', async (stored, languages) => {
    const { resolveLang } = await i18n();
    expect(resolveLang).toBeTypeOf('function');
    expect(run(stored, [...languages])).toBe(resolveLang?.(stored, languages));
  });

  it('falls back to the runtime default when storage throws', async () => {
    const { resolveLang } = await i18n();
    const attrs = new Map<string, string>();
    const root = { setAttribute: (k: string, v: string) => void attrs.set(k, v), removeAttribute: (k: string) => void attrs.delete(k) };
    const storage = {
      getItem: () => {
        throw new Error('blocked');
      },
    };
    runInNewContext(source, { document: { documentElement: root }, localStorage: storage, navigator: { languages: ['ru-RU'] } });
    expect(attrs.get('lang')).toBe(resolveLang?.(null, ['ru-RU']));
  });

  it('index.html keeps a static <html lang="en"> as the fallback', () => {
    expect(readFileSync(`${webRoot}index.html`, 'utf8')).toMatch(/<html lang="en">/);
  });
});
