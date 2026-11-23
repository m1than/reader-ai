// WEB-UI FR-UI-16: the stored theme on <html> before first paint. A classic external script loaded from <head> (the
// app CSP allows 'self' scripts and no inline one). Same rule as web/src/ui/theme.ts: light or dark set data-theme,
// anything else (or a blocked storage) leaves the system scheme.
(function () {
  var value = null;
  try {
    value = localStorage.getItem('reader-ai.theme');
  } catch (e) {
    value = null;
  }
  if (value === 'light' || value === 'dark') document.documentElement.setAttribute('data-theme', value);
  else document.documentElement.removeAttribute('data-theme');
})();
