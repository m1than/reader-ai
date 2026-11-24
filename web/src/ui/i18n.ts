// The interface language (WEB-UI FR-UI-30, FR-UI-31): message catalogues per area as data, a small runtime over
// Intl, the locale choice stored per browser and the lazy Russian catalogues. Each area owns `messages.en.ts`
// (imported statically by its code) and `messages.ru.ts` (reached only through the loaders below, so no Russian module
// sits in an initial chunk). With no provider mounted the runtime is English.
import { createContext, createElement, Fragment, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react';

export const I18N_AREAS = ['app', 'app/observability', 'ui', 'notebook', 'render', 'tutor', 'import', 'import/plan', 'accounts', 'fresh', 'manage'] as const;
export type Area = (typeof I18N_AREAS)[number];
export type Lang = 'en' | 'ru';
export type Choice = 'auto' | 'en' | 'ru';
export const CHOICES: readonly Choice[] = ['auto', 'en', 'ru'];

/** localStorage key of the choice (WEB-MANAGE FR-MANAGE-10's control writes it; theme.js reads it before paint). */
export const LOCALE_KEY = 'reader-ai.locale';

/** The CLDR plural categories of each locale (Intl.PluralRules): a plural message carries exactly these keys. */
export const PLURAL_CATEGORIES = { en: ['one', 'other'], ru: ['one', 'few', 'many', 'other'] } as const satisfies Record<Lang, readonly string[]>;

export type PluralMessage = Readonly<Record<string, string>>;
export type Message = string | PluralMessage;
export type Catalogue = Readonly<Record<string, Message>>;
/** A translation of an English catalogue: the same keys, each a message of its own locale. */
export type Translation<C extends Catalogue> = { readonly [K in keyof C]: Message };
export type Value = string | number | Date | null | undefined;
export type Values = Readonly<Record<string, Value>>;

// The Russian catalogues, one loader per area (FR-UI-30), each its own lazy chunk. The table is declared in
// ./ru-loaders.ts and registered by the entry (main.tsx; the Vitest setup in tests), so the dynamic imports sit in the
// entry chunk only (NFR-UI-1: no second chunk needs Vite's preload helper).
export type RuLoaders = Readonly<Record<Area, () => Promise<{ default: Catalogue }>>>;
let RU: Partial<RuLoaders> = {};
export const registerRuLoaders = (loaders: RuLoaders): void => {
  RU = loaders;
};

/** The areas of the shell, loaded before the first commit when the interface is Russian (FR-UI-31). */
export const SHELL_AREAS: readonly Area[] = ['app', 'ui'];

const primary = (tag: string | undefined): string => (tag ?? '').split('-')[0]!.toLowerCase();

/** `en` and `ru` are explicit; `auto`, a missing or an unknown value follow the browser's first language. */
export const resolveLang = (stored: string | null | undefined, languages: readonly string[] | undefined): Lang => {
  if (stored === 'en' || stored === 'ru') return stored;
  return primary(languages?.[0]) === 'ru' ? 'ru' : 'en';
};

/** The browser's full tag when its primary subtag is the chosen language, else the bare tag. */
export const formatLocale = (lang: Lang, languages: readonly string[] | undefined): string => {
  const tag = languages?.[0];
  if (!tag || primary(tag) !== lang) return lang;
  try {
    return Intl.getCanonicalLocales(tag).length > 0 ? tag : lang;
  } catch {
    return lang;
  }
};

const fill = (text: string, locale: string, values: Values | undefined): string =>
  text.replace(/\{(\w+)\}/g, (whole, name: string) => {
    const v = values?.[name];
    if (v === undefined || v === null) return whole;
    if (typeof v === 'number') return new Intl.NumberFormat(locale, { maximumFractionDigits: 20 }).format(v);
    if (v instanceof Date) return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(v);
    return v;
  });

/** One message with its values: a plural message picks its form by `count`; placeholders are filled as text. */
export const formatMessage = (lang: Lang, locale: string, message: Message, values?: Values): string => {
  if (typeof message === 'string') return fill(message, locale, values);
  const count = typeof values?.count === 'number' ? values.count : 0;
  const category = new Intl.PluralRules(lang).select(count);
  return fill(message[category] ?? message.other ?? '', locale, values);
};

/** A server enum value through its area's catalogue (`enum.<name>.<value>`); an unknown value as received. */
export const enumText = (catalogue: Catalogue, name: string, value: string): string => {
  const key = `enum.${name}.${value}`;
  const m = Object.hasOwn(catalogue, key) ? catalogue[key] : undefined;
  return typeof m === 'string' ? m : value;
};

/** A translator bound to one area's catalogues. */
export interface T<C extends Catalogue> {
  (key: keyof C & string, values?: Values): string;
  /** A server enum value (FR-UI-30): translated when the catalogue maps it, else shown as received. */
  enum: (name: string, value: string) => string;
  /** A key computed at run time: its message when the catalogue has it, else `fallback` (default: the key). */
  maybe: (key: string, values?: Values, fallback?: string) => string;
  lang: Lang;
  locale: string;
}

export const makeT = <C extends Catalogue>(en: C, ru: Catalogue | undefined, lang: Lang, locale: string): T<C> => {
  const active: Catalogue = ru ?? en;
  const msgLang: Lang = ru ? lang : 'en';
  const msgLocale = ru ? locale : lang === 'en' ? locale : 'en';
  const lookup = (key: string): Message | undefined => (Object.hasOwn(active, key) ? active[key] : Object.hasOwn(en, key) ? en[key] : undefined);
  const t = ((key: string, values?: Values) => {
    const m = lookup(key);
    return m === undefined ? key : formatMessage(msgLang, msgLocale, m, values);
  }) as T<C>;
  t.enum = (name, value) => enumText(active, name, value);
  t.maybe = (key, values, fallback) => {
    const m = lookup(key);
    return m === undefined ? (fallback ?? key) : formatMessage(msgLang, msgLocale, m, values);
  };
  t.lang = ru ? lang : 'en';
  t.locale = msgLocale;
  return t;
};

/** English only, for pure functions called outside React (their tests read English). */
export const englishT = <C extends Catalogue>(en: C): T<C> => makeT(en, undefined, 'en', 'en');

// ---- Store: loaded Russian catalogues and the failure flag (per page load).
const loaded = new Map<Area, Catalogue>();
let failed = false;
let version = 0;
let active: Lang = 'en';
const listeners = new Set<() => void>();
const emit = () => {
  version += 1;
  for (const l of listeners) l();
};
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};
const getVersion = () => version;

const setHtmlLang = (lang: Lang) => {
  if (typeof document !== 'undefined') document.documentElement.lang = lang;
};

/** Loads the Russian catalogues of these areas. A failure switches this page load to English (FR-UI-31): html
 * lang=en, the stored choice kept, a new try on the next load. Resolves false then, never rejects. */
export const loadAreas = async (areas: readonly Area[]): Promise<boolean> => {
  if (failed) return false;
  const missing = areas.filter((a) => !loaded.has(a));
  if (missing.length === 0) return true;
  try {
    const mods = await Promise.all(
      missing.map((a) => {
        const load = RU[a];
        return load ? load() : Promise.reject(new Error(`no loader for ${a}`));
      }),
    );
    missing.forEach((a, i) => loaded.set(a, mods[i]!.default));
    emit();
    return true;
  } catch {
    failed = true;
    active = 'en';
    setHtmlLang('en');
    emit();
    return false;
  }
};

/** For a lazy route: its chunk and, in a Russian interface, its areas' catalogues, in one Promise.all. */
export const withAreas = <M,>(chunk: () => Promise<M>, areas: readonly Area[]): (() => Promise<M>) => () =>
  Promise.all([chunk(), active === 'ru' ? loadAreas(areas) : Promise.resolve(true)]).then(([m]) => m);

const storage = (): Storage | null => {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
};
const readStored = (): string | null => {
  try {
    return storage()?.getItem(LOCALE_KEY) ?? null;
  } catch {
    return null;
  }
};
const toChoice = (v: string | null): Choice => (v === 'en' || v === 'ru' || v === 'auto' ? v : 'auto');
const browserLanguages = (): readonly string[] => (typeof navigator !== 'undefined' && navigator.languages ? navigator.languages : []);

/** Before the first React commit (main.tsx): English renders at once; Russian waits for the shell's catalogues. */
export const bootLocale = (): Promise<unknown> | null => {
  const lang = resolveLang(readStored(), browserLanguages());
  if (lang === 'en') return null;
  active = 'ru';
  return loadAreas(SHELL_AREAS);
};

export interface LocaleState {
  lang: Lang;
  choice: Choice;
  formatLocale: string;
  setChoice: (c: Choice) => Promise<void>;
}

const LocaleContext = createContext<LocaleState>({ lang: 'en', choice: 'auto', formatLocale: 'en', setChoice: async () => undefined });

export const useLocale = (): LocaleState => useContext(LocaleContext);

/** Holds the choice (auto is resolved once per page load: later `languagechange` events are ignored, FR-UI-30) and
 * keeps `html[lang]` equal to the interface language (FR-UI-31). */
export function I18nProvider({ children, languages }: { children?: ReactNode; languages?: readonly string[] }) {
  const [langs] = useState(() => languages ?? browserLanguages());
  const [choice, setChoiceState] = useState<Choice>(() => toChoice(readStored()));
  useSyncExternalStore(subscribe, getVersion, getVersion);
  const wanted = resolveLang(choice, langs);
  const lang: Lang = wanted === 'ru' && !failed ? 'ru' : 'en';
  active = lang;
  useLayoutEffect(() => setHtmlLang(lang), [lang]);
  useEffect(() => {
    if (lang === 'ru') void loadAreas(SHELL_AREAS);
  }, [lang]);
  const setChoice = useCallback(
    async (c: Choice) => {
      try {
        storage()?.setItem(LOCALE_KEY, c);
      } catch {
        // storage blocked: the choice holds for this page only
      }
      if (resolveLang(c, langs) === 'ru') {
        active = 'ru';
        await loadAreas(I18N_AREAS);
      }
      setChoiceState(c);
    },
    [langs],
  );
  const value = useMemo<LocaleState>(() => ({ lang, choice, formatLocale: formatLocale(lang, langs), setChoice }), [lang, choice, langs, setChoice]);
  return createElement(LocaleContext.Provider, { value }, children);
}

/** The translator of one area (FR-UI-30). In a Russian interface an area whose catalogue is not loaded yet renders
 * English and loads it (lazy routes load theirs with their chunk, so this is a fallback). */
export const useT = <C extends Catalogue>(area: Area, en: C): T<C> => {
  const { lang, formatLocale: locale } = useContext(LocaleContext);
  useSyncExternalStore(subscribe, getVersion, getVersion);
  const ru = lang === 'ru' ? loaded.get(area) : undefined;
  useEffect(() => {
    if (lang === 'ru' && !loaded.has(area)) void loadAreas([area]);
  }, [lang, area]);
  return useMemo(() => makeT(en, ru, lang, locale), [en, ru, lang, locale]);
};

/** A translated sentence whose remaining `{name}` placeholders are elements (a code span, a link): the text around
 * them stays text nodes (INV-UI-2). */
export const interleave = (text: string, nodes: Readonly<Record<string, ReactNode>>): ReactNode[] =>
  text.split(/(\{\w+\})/g).map((part, i) => {
    const m = /^\{(\w+)\}$/.exec(part);
    return m && Object.hasOwn(nodes, m[1]!) ? createElement(Fragment, { key: i }, nodes[m[1]!]) : part;
  });

// ---- Language of user-written text (FR-UI-32): `ru` when Cyrillic letters are more than half of the letters.
const LETTER = /\p{L}/gu;
const CYRILLIC = /\p{Script=Cyrillic}/gu;
export const textLang = (text: string): Lang => {
  const letters = text.match(LETTER)?.length ?? 0;
  const cyrillic = text.match(CYRILLIC)?.length ?? 0;
  return letters > 0 && cyrillic * 2 > letters ? 'ru' : 'en';
};
