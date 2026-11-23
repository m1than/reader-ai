// The lesson's language for `lang` on the lesson container (WEB-UI FR-UI-10): `ru` when Cyrillic letters are more
// than half of the letters in the markdown sources, else `en`. Code cells do not count.
const LETTER = /\p{L}/gu;
const CYRILLIC = /\p{Script=Cyrillic}/gu;

export const lessonLang = (nb: { cells: { cell_type: string; source: string | string[] }[] }): 'ru' | 'en' => {
  const text = nb.cells
    .filter((c) => c.cell_type === 'markdown')
    .map((c) => (Array.isArray(c.source) ? c.source.join('') : c.source))
    .join('\n');
  const letters = text.match(LETTER)?.length ?? 0;
  const cyrillic = text.match(CYRILLIC)?.length ?? 0;
  return letters > 0 && cyrillic * 2 > letters ? 'ru' : 'en';
};
