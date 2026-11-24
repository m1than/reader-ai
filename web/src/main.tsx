// Browser entry (WEB-UI): library styles, Calm, KaTeX's stylesheet (fonts emitted as files, FR-UI-9), then the app.
import '@acme/ui/styles.css';
import './ui/tokens/calm.css';
import 'katex/dist/katex.min.css';
import './ui/app.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppRoot } from './app/AppRoot';
import { bootLocale } from './ui/i18n';
import './ui/ru-loaders';

const render = () =>
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <AppRoot />
    </StrictMode>,
  );

// WEB-UI FR-UI-31: the first commit waits for the shell's Russian catalogues when the interface is Russian; English
// renders at once. A failed load renders English (the runtime switches html[lang] back to en).
const boot = bootLocale();
if (boot) void boot.then(render, render);
else render();
