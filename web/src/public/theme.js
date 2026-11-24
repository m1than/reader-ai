// WEB-UI FR-UI-16: the stored theme on <html> before first paint. A classic external script loaded from <head> (the
// app CSP allows 'self' scripts and no inline one); it lives in src/public (Vite's publicDir), so the production image,
// which copies src, serves it at /theme.js. Same rule as web/src/ui/theme.ts: light or dark set data-theme, anything
// else (or a blocked storage) leaves the system scheme.
// WEB-UI FR-UI-31: the interface language on <html lang> with the runtime's rule (web/src/ui/i18n.ts resolveLang):
// a stored `en` or `ru` wins; `auto`, a missing or an unknown value follow the primary subtag of the browser's first
// language, compared case-insensitively.
(function () {
  var root = document.documentElement;
  var value = null;
  try {
    value = localStorage.getItem('reader-ai.theme');
  } catch (e) {
    value = null;
  }
  if (value === 'light' || value === 'dark') root.setAttribute('data-theme', value);
  else root.removeAttribute('data-theme');
  var locale = null;
  try {
    locale = localStorage.getItem('reader-ai.locale');
  } catch (e) {
    locale = null;
  }
  var lang = 'en';
  if (locale === 'en' || locale === 'ru') lang = locale;
  else {
    var first = (typeof navigator !== 'undefined' && navigator.languages && navigator.languages[0]) || '';
    if (String(first).split('-')[0].toLowerCase() === 'ru') lang = 'ru';
  }
  root.setAttribute('lang', lang);
  // WEB-MANAGE FR-MANAGE-6: text size and reading width (web/src/manage/appearance.ts's rule): a known non-default
  // value sets its attribute; the default or an unknown value leaves the stylesheet's own values.
  var prefs = [
    ['reader-ai.text-size', 'data-text-size', ['small', 'large']],
    ['reader-ai.width', 'data-width', ['narrow', 'wide']],
  ];
  for (var i = 0; i < prefs.length; i += 1) {
    var stored = null;
    try {
      stored = localStorage.getItem(prefs[i][0]);
    } catch (e) {
      stored = null;
    }
    if (prefs[i][2].indexOf(stored) >= 0) root.setAttribute(prefs[i][1], stored);
    else root.removeAttribute(prefs[i][1]);
  }
})();
