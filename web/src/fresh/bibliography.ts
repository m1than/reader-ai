// Client checks for the bibliography form (WEB-FRESH FR-FRESHUI-2) with SRV-FRESH FR-FRESH-1's bounds. The server
// checks again and answers 400 `bibliography_invalid` with the field.
import type { Bibliography, BibliographyInput } from './api';

export const BOUNDS = { title: 200, authors: 10, author: 100, edition: 40, publisher: 120, yearMin: 1450 } as const;

export type Field = 'title' | 'authors' | 'edition' | 'year' | 'publisher' | 'isbn' | 'original_title' | 'original_edition' | 'original_year';

/** The form's text values, one per field; authors one per line. */
export type FormValues = Record<Field, string>;

export const FIELDS: ReadonlyArray<{ field: Field; label: string; multiline?: boolean }> = [
  { field: 'title', label: 'Title' },
  { field: 'authors', label: 'Authors (one per line)', multiline: true },
  { field: 'edition', label: 'Edition' },
  { field: 'year', label: 'Year' },
  { field: 'publisher', label: 'Publisher' },
  { field: 'isbn', label: 'ISBN' },
  { field: 'original_title', label: 'Original title' },
  { field: 'original_edition', label: 'Original edition' },
  { field: 'original_year', label: 'Original year' },
];

export const MESSAGES: Record<Field, string> = {
  title: `Enter a title of up to ${BOUNDS.title} characters.`,
  authors: `Enter up to ${BOUNDS.authors} authors, one per line, each up to ${BOUNDS.author} characters.`,
  edition: `Up to ${BOUNDS.edition} characters.`,
  year: 'A year from 1450 to next year.',
  publisher: `Up to ${BOUNDS.publisher} characters.`,
  isbn: 'A valid ISBN-10 or ISBN-13.',
  original_title: `Up to ${BOUNDS.title} characters.`,
  original_edition: `Up to ${BOUNDS.edition} characters.`,
  original_year: 'A year from 1450 to next year.',
};

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
): { body: BibliographyInput; errors: Partial<Record<Field, string>> } => {
  const errors: Partial<Record<Field, string>> = {};
  const title = f.title.trim();
  if (title === '' || title.length > BOUNDS.title) errors.title = MESSAGES.title;
  const authors = f.authors.split('\n').map((a) => a.trim()).filter((a) => a !== '');
  if (authors.length > BOUNDS.authors || authors.some((a) => a.length > BOUNDS.author)) errors.authors = MESSAGES.authors;
  const short = (field: Field, max: number) => {
    const v = optional(f[field]);
    if (v !== null && v.length > max) errors[field] = MESSAGES[field];
    return v;
  };
  const year = (field: Field) => {
    const v = optional(f[field]);
    if (v === null) return null;
    const n = /^\d{4}$/.test(v) ? Number(v) : NaN;
    if (!(n >= BOUNDS.yearMin && n <= thisYear + 1)) errors[field] = MESSAGES[field];
    return n;
  };
  const isbnRaw = optional(f.isbn);
  if (isbnRaw !== null && !isbnValid(isbnRaw)) errors.isbn = MESSAGES.isbn;
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
