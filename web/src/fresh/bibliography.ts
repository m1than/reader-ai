// Client checks for the bibliography form (WEB-FRESH FR-FRESHUI-2) with SRV-FRESH FR-FRESH-1's bounds. The server
// checks again and answers 400 `bibliography_invalid` with the field.
// The copy is this area's catalogue (WEB-UI FR-UI-30): English by default, or the translator the card passes.
import { englishT, type T } from '../ui/i18n';
import type { Bibliography, BibliographyInput } from './api';
import en from './messages.en';

export type FreshT = T<typeof en>;
const english: FreshT = englishT(en);

export const BOUNDS = { title: 200, authors: 10, author: 100, edition: 40, publisher: 120, yearMin: 1450 } as const;

export type Field = 'title' | 'authors' | 'edition' | 'year' | 'publisher' | 'isbn' | 'original_title' | 'original_edition' | 'original_year';

/** The form's text values, one per field; authors one per line. */
export type FormValues = Record<Field, string>;

export const FIELDS: ReadonlyArray<{ field: Field; multiline?: boolean }> = [
  { field: 'title' },
  { field: 'authors', multiline: true },
  { field: 'edition' },
  { field: 'year' },
  { field: 'publisher' },
  { field: 'isbn' },
  { field: 'original_title' },
  { field: 'original_edition' },
  { field: 'original_year' },
];

/** A field's label. */
export const fieldLabel = (field: Field, t: FreshT = english): string => t(`field.${field}`);

/** A field's check message. */
export const fieldMessage = (field: Field, t: FreshT = english): string => {
  switch (field) {
    case 'title':
      return t('error.title', { max: BOUNDS.title });
    case 'authors':
      return t('error.authors', { max: BOUNDS.authors, each: BOUNDS.author });
    case 'edition':
    case 'original_edition':
      return t('error.short', { max: BOUNDS.edition });
    case 'publisher':
      return t('error.short', { max: BOUNDS.publisher });
    case 'original_title':
      return t('error.short', { max: BOUNDS.title });
    case 'year':
    case 'original_year':
      return t('error.year');
    case 'isbn':
      return t('error.isbn');
  }
};

/** The English messages per field. */
export const MESSAGES: Record<Field, string> = Object.fromEntries(FIELDS.map(({ field }) => [field, fieldMessage(field)])) as Record<Field, string>;

const text = (s: string | null | undefined) => s ?? '';

export const toForm = (b: Omit<Bibliography, 'source'> | null): FormValues => ({
  title: text(b?.title),
  authors: (b?.authors ?? []).join('\n'),
  edition: text(b?.edition),
  year: b?.year == null ? '' : String(b.year),
  publisher: text(b?.publisher),
  isbn: text(b?.isbn),
  original_title: text(b?.original_title),
  original_edition: text(b?.original_edition),
  original_year: b?.original_year == null ? '' : String(b.original_year),
});

export const isbnValid = (raw: string): boolean => {
  const s = raw.replace(/[\s-]/g, '').toUpperCase();
  if (/^\d{9}[\dX]$/.test(s)) {
    const sum = [...s].reduce((acc, c, i) => acc + (10 - i) * (c === 'X' ? 10 : Number(c)), 0);
    return sum % 11 === 0;
  }
  if (/^\d{13}$/.test(s)) {
    const sum = [...s].reduce((acc, c, i) => acc + (i % 2 === 0 ? 1 : 3) * Number(c), 0);
    return sum % 10 === 0;
  }
  return false;
};

const optional = (s: string): string | null => (s.trim() === '' ? null : s.trim());

/** The PUT body, or the fields that fail the client checks. `keep` carries the fields the form does not edit. */
export const fromForm = (
  f: FormValues,
  keep: { original_authors: string[] | null },
  thisYear: number,
  t: FreshT = english,
): { body: BibliographyInput; errors: Partial<Record<Field, string>> } => {
  const errors: Partial<Record<Field, string>> = {};
  const msg = (field: Field) => fieldMessage(field, t);
  const title = f.title.trim();
  if (title === '' || title.length > BOUNDS.title) errors.title = msg('title');
  const authors = f.authors.split('\n').map((a) => a.trim()).filter((a) => a !== '');
  if (authors.length > BOUNDS.authors || authors.some((a) => a.length > BOUNDS.author)) errors.authors = msg('authors');
  const short = (field: Field, max: number) => {
    const v = optional(f[field]);
    if (v !== null && v.length > max) errors[field] = msg(field);
    return v;
  };
  const year = (field: Field) => {
    const v = optional(f[field]);
    if (v === null) return null;
    const n = /^\d{4}$/.test(v) ? Number(v) : NaN;
    if (!(n >= BOUNDS.yearMin && n <= thisYear + 1)) errors[field] = msg(field);
    return n;
  };
  const isbnRaw = optional(f.isbn);
  if (isbnRaw !== null && !isbnValid(isbnRaw)) errors.isbn = msg('isbn');
  const body: BibliographyInput = {
    title,
    authors,
    edition: short('edition', BOUNDS.edition),
    year: year('year'),
    publisher: short('publisher', BOUNDS.publisher),
    isbn: isbnRaw === null ? null : isbnRaw.replace(/[\s-]/g, '').toUpperCase(),
    original_title: short('original_title', BOUNDS.title),
    original_authors: keep.original_authors,
    original_edition: short('original_edition', BOUNDS.edition),
    original_year: year('original_year'),
  };
  return { body, errors };
};

/** A server field name mapped to the form's field (original_authors has no input; it is shown on the title). */
export const formField = (field: unknown): Field | null =>
  typeof field === 'string' && FIELDS.some((x) => x.field === field) ? (field as Field) : null;
