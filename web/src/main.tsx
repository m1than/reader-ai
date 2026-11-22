// Browser entry (WEB-UI): library styles, Calm, KaTeX's stylesheet (fonts emitted as files, FR-UI-9), then the app.
import '@acme/ui/styles.css';
import './ui/tokens/calm.css';
import 'katex/dist/katex.min.css';
import './ui/app.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppRoot } from './app/AppRoot';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppRoot />
  </StrictMode>,
);
