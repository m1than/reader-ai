// The planning part's messages, keyed by code (WEB-PLAN FR-IMPORT-5, -6): plain text only, no server text shown.
// The copy lives in messages.en.ts and messages.ru.ts (WEB-UI FR-UI-30); these helpers read it in English by default.
import { englishT, type T } from '../../ui/i18n';
import en from './messages.en';

export type PlanT = T<typeof en>;
const english = englishT(en);

export const MESSAGES: Record<string, string> = Object.fromEntries(
  Object.entries(en)
    .filter(([k]) => k.startsWith('code.'))
    .map(([k, v]) => [k.slice('code.'.length), v as string]),
);

export const message = (code: string, t: PlanT = english): string =>
  Object.hasOwn(MESSAGES, code) ? t.maybe(`code.${code}`) : t('code.unknown');
