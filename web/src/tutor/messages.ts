// The tutor error catalogue (SRV-TUTOR `errors`, VEC-TUTOR-5): every code has one fixed message; unknown codes
// show tutor_failed's. The messages live in this area's catalogues (WEB-UI FR-UI-30); `messageFor` takes the
// active translator and reads English without one.
import { englishT, type T } from '../ui/i18n';
import en from './messages.en';

const PREFIX = 'code.';

export const CODES: readonly string[] = Object.keys(en)
  .filter((k) => k.startsWith(PREFIX))
  .map((k) => k.slice(PREFIX.length));

const known = new Set(CODES);
const english = englishT(en);

export const messageFor = (code: string, t: T<typeof en> = english): string =>
  t(`${PREFIX}${known.has(code) ? code : 'tutor_failed'}` as keyof typeof en);
