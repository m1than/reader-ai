// One plain-text catalogue keyed by code (WEB-IMPORT FR-IMPORT-7): client checks, SRV-INGEST codes, network and
// response failures, and an `unknown` fallback. No server text is ever rendered. The copy lives in messages.en.ts and
// messages.ru.ts (WEB-UI FR-UI-30); these helpers read it in English by default, or through a given translator.
import { englishT, type T } from '../ui/i18n';
import en from './messages.en';

export type ImportT = T<typeof en>;
const english = englishT(en);

export const CODES: readonly string[] = Object.keys(en)
  .filter((k) => k.startsWith('code.'))
  .map((k) => k.slice('code.'.length));

export const message = (code: string, t: ImportT = english): string =>
  CODES.includes(code) ? t.maybe(`code.${code}`) : t('code.unknown');

/** FR-IMPORT-3: the cross-run residual of SRV-INGEST FR-INGEST-22 has no other signal than this symptom. */
export const MEMORY_REMEDY = en.memory_remedy;
